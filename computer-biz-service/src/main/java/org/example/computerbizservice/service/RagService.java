package org.example.computerbizservice.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.client.RagClient;
import org.example.computerbizservice.common.AppConstants;
import org.example.computerbizservice.common.enums.ChatMsgStatus;
import org.example.computerbizservice.dto.request.rag.AiChatReq;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.exception.RagApiException;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import reactor.core.publisher.Flux;
import reactor.core.publisher.SignalType;
import reactor.core.scheduler.Schedulers;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 业务编排层：调上游、把结果转给前端，并在旁边落库（轮询、重试以后也写在这一层）。
 *
 * <h2>★ 落库是旁路</h2>
 * 主链路是「浏览器 → Java → FastAPI」，落库挂在旁边。所以：
 * <ul>
 *   <li><b>启动期：缺库要吵</b> —— 见 {@code common/PersistenceStartupCheck}。</li>
 *   <li><b>运行期：写库失败要静音</b> —— 所有落库调用都走 {@link #safeWrite} /
 *       {@link #settleAsync}，失败只打日志。尤其是上传那条路：上游没有幂等键，
 *       我们自己的说明文档里就写着「客户端误判会导致同一文档入库两次」——
 *       一个 DB 抖动绝不能被翻译成 500，那会诱导用户重传。</li>
 * </ul>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class RagService {

    private final RagClient ragClient;
    private final IngestRecordService ingestRecordService;
    private final ChatRecordService chatRecordService;

    public FileUploadResp upload(MultipartFile file, FileUploadReq extra) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("file 不能为空");
        }
        // 先调上游：job_id 是上游给的，调之前拿不到；上游失败会直接抛给前端，也就没有「任务」可记
        FileUploadResp resp = ragClient.upload(file, extra);
        // 再落库。顺序不能反，而且落库失败不影响返回值（见类注释）
        safeWrite("上传受理落库", resp == null ? "(无 job_id)" : resp.getJobId(),
                () -> ingestRecordService.begin(resp, file));
        return resp;
    }

    public TaskQueryResp queryTask(String jobId) {
        if (jobId == null || jobId.isBlank()) {
            throw new IllegalArgumentException("jobId 不能为空");
        }
        TaskQueryResp resp;
        try {
            resp = ragClient.queryTask(jobId);
        } catch (RagApiException e) {
            // ★ 轮询失败 ≠ 入库失败。这条异常原样抛给前端，同时把「没问到什么」记下来 ——
            //   但**不改 status**，那个列只记录上游说过的话（见 IngestRecordService 的注释）
            safeWrite("轮询失败落库", jobId, () -> ingestRecordService.markPollFailed(jobId, e));
            throw e;
        }
        safeWrite("轮询结果落库", jobId, () -> ingestRecordService.applyPoll(resp));
        return resp;
    }

    /**
     * 问答：把上游的 SSE 流原样转给前端，同时<b>在旁边把回答攒下来</b>落库。
     *
     * <p>为什么要在 Java 侧攒：上游只会把答案流给我们，浏览器那边的正文也只存在内存里
     * （前端刷新页面就没了，session_id 甚至是刷新即换新的）。不攒的话这条链路不留任何痕迹。
     *
     * <p>为什么要用这个形状来攒（每一处都有代价很具体的理由）：
     * <ul>
     *   <li><b>提问在订阅之前、请求线程上同步写</b>：那时响应还没开始，写库失败还能被抓住记日志；
     *       而且上游挂掉的那次提问也留下了痕迹。放进 {@code Flux.defer()} 里写的话，
     *       落库就成了流的一部分 —— DB 一抖会把用户的回答带崩。</li>
     *   <li><b>只用 {@code doOnNext}/{@code doOnError}/{@code doFinally} 这类副作用算子</b>：
     *       对流是透明的（不改背压、不缓冲、不换线程）。<b>不用 {@code publishOn}</b> ——
     *       那会在上游和 Spring 的写入器之间插一次调度跳转，而且
     *       {@code publishOn(boundedElastic)} 会为整个回答（可能几分钟）长期占住一个弹性线程，
     *       那是线程池饥饿，不是修法。要换线程的是「落库那一件事」，不是流本身。</li>
     *   <li><b>不消费错误</b>：{@code onErrorResume}/{@code onErrorReturn} 会改掉前端看到的东西，
     *       前端那套「从悬空行里捞 detail」的逻辑就失效了。</li>
     * </ul>
     */
    public Flux<String> aiChat(AiChatReq req) {
        // ① 先落「提问」和一行空的「助手」行（一个事务，见 ChatRecordService）
        Long assistantId = null;
        try {
            assistantId = chatRecordService.beginExchange(req, AppConstants.DEFAULT_USER_ID);
        } catch (Exception e) {
            // 落库失败 → 返回 null → 下面原样放行，问答照常
            log.error("落库失败（问答照常进行，数据不会进库）: beginExchange session={}",
                    req == null ? null : req.getSessionId(), e);
        }
        if (assistantId == null) {
            return ragClient.aiChat(req);
        }

        final Long id = assistantId;
        // ② 累加器按「每次订阅」新建：本服务只会订阅一次，但 defer 用一行代价
        //    换掉「多路订阅共用一个缓冲区」这个隐患
        return Flux.defer(() -> {
            StringBuilder answer = new StringBuilder(2048);
            AtomicReference<Throwable> failure = new AtomicReference<>();
            AtomicBoolean settled = new AtomicBoolean(false);
            return ragClient.aiChat(req)
                    // ③ 攒正文：纯内存拼接，O(1) 摊还，不做任何 IO，所以留在事件循环上没关系
                    .doOnNext(piece -> {
                        if (piece != null && answer.length() < AppConstants.MAX_STORED_ANSWER_CHARS) {
                            answer.append(piece);
                        }
                    })
                    // ④ 只记录不消费，终态时统一处理
                    .doOnError(failure::set)
                    .doFinally(signal -> {
                        // doFinally 在边缘情况下可能不止触发一次（完成与取消都到），
                        // 所以宁可加一个闸门，也不要写两次终态
                        if (settled.compareAndSet(false, true)) {
                            settleAsync(id, answer, signal, failure.get());
                        }
                    });
        });
    }

    /**
     * 终态落库。<b>换线程</b>和<b>吞异常</b>这两件事缺一不可。
     *
     * <p>换线程：{@code doFinally} 是在 <b>reactor-netty 事件循环</b>线程上触发的
     * （上游的 chunk 由事件循环投递，Spring 写 SSE 也在同一个线程上干活）。
     * 在那里做阻塞的 JDBC 会卡住<b>所有请求共用</b>的 IO 线程 —— 症状是「平时都好好的，
     * 一并发就整体变慢」，最难查的那类。所以甩到弹性线程池去阻塞。
     *
     * <p>吞异常：一个抛异常的终态回调会把<b>已经成功返回给用户的回答</b>变成报错。
     * 反过来说，落库失败只配打一行日志。{@code answer.toString()} 也放在这个任务里做 ——
     * 在事件循环上拷 10 万字是没理由的开销。
     */
    private void settleAsync(Long assistantId, StringBuilder answer, SignalType signal, Throwable failure) {
        ChatMsgStatus status = classify(signal, answer.length(), failure);
        // 这一行一次问答只打一条，但它是验证整条 tee 最有用的东西：
        // 流的结束方式、收到的字数、异常类名、最终落库状态，全在一句话里
        log.info("问答终态: assistantId={} signal={} 收到={}字 异常={} → 落库 {}",
                assistantId, signal, answer.length(),
                failure == null ? "无" : failure.getClass().getName(), status);
        Schedulers.boundedElastic().schedule(() -> {
            try {
                chatRecordService.settle(assistantId, answer.toString(), status, detailOf(failure, status));
            } catch (Exception e) {
                log.error("落库失败（回答已正常返回用户，数据不会进库）: assistantId={} status={}",
                        assistantId, status, e);
            }
        });
    }

    /**
     * 流的结束方式 → 落库状态。这个判断只能放在这里 —— 只有它知道「收到过内容没有」
     * 和「是怎么结束的」。
     *
     * <pre>
     *   ON_COMPLETE             → DONE
     *   CANCEL                  → CANCELED   前端点「停止生成」= abort fetch = 订阅被取消
     *   ON_ERROR + 客户端断开    → CANCELED   同上，只是以写失败的形式冒出来（不是故障）
     *   ON_ERROR + 一个字都没收到 → ERROR
     *   ON_ERROR + 有内容        → INTERRUPTED
     * </pre>
     *
     * <p>★ 这五档和前端的 {@code chatStore.MSG} 是同一套语义，别自己改口径。
     *
     * <p>★ 常量名是 {@code CANCEL} 不是 {@code ON_CANCEL}：Reactor 3.8 把后者去掉了
     * （老版本的 {@code SignalType} 里叫 {@code ON_CANCEL}，抄老例子会编译不过）。
     *
     * <p>★ 一条<b>待验证</b>的事：客户端断开到底以哪种形态到达（{@code CANCEL}
     * 还是带着 Tomcat {@code ClientAbortException} 的 {@code ON_ERROR}），我按两种都处理了，
     * 但本地没有实测过。验证方法就是前端点一次「停止生成」，然后看
     * {@link #settleAsync} 打的那行「问答终态」日志里的 signal 和异常类名 ——
     * 如果落成了 INTERRUPTED，就照着日志里那个类名把下面的判断补准。
     */
    private static ChatMsgStatus classify(SignalType signal, int textLength, Throwable failure) {
        if (signal == SignalType.ON_COMPLETE) {
            return ChatMsgStatus.DONE;
        }
        if (signal == SignalType.CANCEL || isClientAbort(failure)) {
            return ChatMsgStatus.CANCELED;
        }
        return textLength == 0 ? ChatMsgStatus.ERROR : ChatMsgStatus.INTERRUPTED;
    }

    /**
     * 判断异常是不是「客户端把连接断了」。
     *
     * <p>只认类名里的 ClientAbort（Tomcat 的 ClientAbortException 等），
     * <b>不能</b>把 IOException 一律当成断开 —— 上游挂掉时读失败同样是 IOException，
     * 那种情况应该是 INTERRUPTED（收到一半被打断），不是 CANCELED（用户主动停）。
     */
    private static boolean isClientAbort(Throwable failure) {
        for (Throwable t = failure; t != null; t = t.getCause()) {
            if (t.getClass().getName().contains("ClientAbort")) {
                return true;
            }
            if (t == t.getCause()) {
                break; // 自环，防呆
            }
        }
        return false;
    }

    /**
     * 终态原因文案。
     *
     * <p>★ 取消和正常结束时返回<b>空串而不是 null</b>：MP 默认不把 null 字段写进 UPDATE，
     * 写 null 等于「这列不动」，上一次残留的报错会永远挂在这行上。清空只能靠空串。
     */
    private static String detailOf(Throwable failure, ChatMsgStatus status) {
        if (status == ChatMsgStatus.CANCELED || status == ChatMsgStatus.DONE || failure == null) {
            return "";
        }
        // RagApiException 的 detail 已经被 fromUpstream 翻译过了 —— 错误详情只能有一份实现
        if (failure instanceof RagApiException rae) {
            return rae.getDetail();
        }
        String message = failure.getMessage();
        return failure.getClass().getSimpleName() + (message == null ? "" : ": " + message);
    }

    /**
     * 落库调用统一出口：失败只记日志。
     *
     * <p>这是「落库是旁路」在代码里唯一需要记住的一条：<b>永远不要让落库的异常跑到调用方</b>
     * （尤其上传那条路，见类注释里关于重复入库的说明）。
     *
     * @param what 出错的环节，写在日志里方便定位
     * @param key  业务键（job_id / session_id / assistantId），出问题时靠它去库里对账
     */
    private static void safeWrite(String what, String key, Runnable action) {
        try {
            action.run();
        } catch (Exception e) {
            log.error("落库失败（{}，主链路不受影响，数据不会进库）: {}", what, key, e);
        }
    }
}
