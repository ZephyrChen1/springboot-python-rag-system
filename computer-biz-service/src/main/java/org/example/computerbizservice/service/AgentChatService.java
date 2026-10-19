package org.example.computerbizservice.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.client.RagClient;
import org.example.computerbizservice.common.AppConstants;
import org.example.computerbizservice.common.enums.ChatMsgStatus;
import org.example.computerbizservice.dto.agent.AgentEventView;
import org.example.computerbizservice.dto.request.rag.AgentChatReq;
import org.example.computerbizservice.exception.RagApiException;
import org.example.computerbizservice.util.TextUtils;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClientRequestException;

import reactor.core.publisher.Flux;
import reactor.core.publisher.SignalType;
import reactor.core.scheduler.Schedulers;

import tools.jackson.databind.ObjectMapper;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Pattern;

/**
 * Agentic RAG 问答的编排层。
 *
 * <h2>主链路和旁路</h2>
 * <pre>
 *   浏览器 ← SSE 帧 ← Java ← NDJSON 行 ← FastAPI
 *                        │
 *                        ├─ 攒 answer 增量        → chat_message（终态一次写入）
 *                        └─ 解析 thought/tool_*   → chat_step（异步、按序）
 * </pre>
 * <p><b>转发永远是原样的一行</b>：本类不重新序列化事件，也不过滤类型。前端拿到的是
 * Python 那一行 JSON。原因和 {@code chat_message.content} 存上游原始文本一样 ——
 * 在 Java 侧再实现一遍事件协议，就是在维护第二份前端契约。
 *
 * <h2>三个容易踩的点</h2>
 * <ul>
 *   <li><b>HTTP 200 不等于成功</b>：Python 侧很多失败（BUSY、MODEL_ERROR、MAX_STEPS）
 *       是先发一个 {@code error} 事件再正常关流，所以终态判定必须看「见没见过 done 事件」，
 *       不能只看 Reactor 的 ON_COMPLETE。</li>
 *   <li><b>不能让阻塞 JDBC 留在事件循环上</b>：{@code doOnNext} 由 reactor-netty 线程调用，
 *       落库一律经 {@link AgentStepService} 的单线程队列 / {@link Schedulers#boundedElastic()}。</li>
 *   <li><b>落库失败不能影响回答</b>：旁路，全部吞成日志。</li>
 * </ul>
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class AgentChatService {

    /** 和 Python 的 AgentChatRequest.session_id 保持同一口径 */
    private static final Pattern SESSION_ID = Pattern.compile("^[A-Za-z0-9_-]{1,128}$");

    /** 和 Python 的 AgentChatRequest.message max_length 对齐 */
    private static final int MAX_MESSAGE_CHARS = 20_000;

    private final RagClient ragClient;
    private final ChatRecordService chatRecordService;
    private final AgentStepService agentStepService;
    private final ObjectMapper objectMapper; // 只用于把上游的一行解析成 AgentEventView

    public Flux<String> chat(AgentChatReq req) {
        validate(req);

        // ① 提问 + 空的助手行，在请求线程上同步落库（和 /api/rag/chat 同一条规则：
        //    这时响应还没开始，写库失败还能被抓住记日志）
        Long assistantId = null;
        try {
            assistantId = chatRecordService.beginExchange(
                    req.getSessionId(), req.getMessage(), AppConstants.DEFAULT_USER_ID);
        } catch (Exception e) {
            log.error("落库失败（agent 问答照常进行，数据不会进库）: beginExchange session={}",
                    req.getSessionId(), e);
        }
        final Long id = assistantId;

        // ② 累加器按「每次订阅」新建
        return Flux.defer(() -> {
            StringBuilder answer = new StringBuilder(2048);
            // call_id → tool_call 事件的 ts（毫秒）。只在事件循环线程上读写，不需要并发容器
            Map<String, Long> callStartedAt = new HashMap<>();
            AtomicReference<Throwable> failure = new AtomicReference<>();
            AtomicReference<String> upstreamError = new AtomicReference<>("");
            AtomicBoolean sawDone = new AtomicBoolean(false);
            AtomicBoolean settled = new AtomicBoolean(false);

            return ragClient.agentChat(req, AppConstants.DEFAULT_USER_ID)
                    // ③ 纯内存旁路：解析、攒正文、排轨迹写入。不消费、不改变事件本身
                    .doOnNext(line -> observe(line, id, req.getSessionId(), answer,
                            callStartedAt, upstreamError, sawDone))
                    // 连不上上游不是「服务内部错误」：翻成 502，让前端看到真实原因。
                    // 放在 doOnError 之前，落库记录的也是翻译后的文案。
                    .onErrorMap(AgentChatService::translateUpstreamError)
                    .doOnError(failure::set)
                    .doFinally(signal -> {
                        if (settled.compareAndSet(false, true)) {
                            settleAsync(id, answer, signal, failure.get(),
                                    sawDone.get(), upstreamError.get());
                        }
                    });
        });
    }

    private static void validate(AgentChatReq req) {
        if (req == null || TextUtils.isBlank(req.getSessionId()) || TextUtils.isBlank(req.getMessage())) {
            throw new IllegalArgumentException("session_id / message 不能为空");
        }
        if (!SESSION_ID.matcher(req.getSessionId()).matches()) {
            throw new IllegalArgumentException("session_id 只允许 1~128 位字母、数字、下划线、连字符");
        }
        if (req.getMessage().length() > MAX_MESSAGE_CHARS) {
            throw new IllegalArgumentException("message 超过 " + MAX_MESSAGE_CHARS + " 字");
        }
    }

    /**
     * 处理一行 NDJSON（副作用，不改动这一行）。
     *
     * <p>解析失败只记日志：这一行仍然会被下游转发给前端，可能是协议扩张后 Java 还不认识的
     * 类型 —— 不该因为旁路看不懂就丢掉用户看到的内容。
     */
    private void observe(String line, Long assistantId, String sessionId, StringBuilder answer,
                         Map<String, Long> callStartedAt,
                         AtomicReference<String> upstreamError,
                         AtomicBoolean sawDone) {
        AgentEventView event;
        try {
            event = objectMapper.readValue(line, AgentEventView.class);
        } catch (Exception e) {
            log.warn("无法解析上游 agent 事件（仍会原样转发）: {}",
                    TextUtils.truncate(line, 200));
            return;
        }
        if (event == null || event.getType() == null) {
            return;
        }

        switch (event.getType()) {
            case "answer" -> {
                if (event.getDelta() != null
                        && answer.length() < AppConstants.MAX_STORED_ANSWER_CHARS) {
                    answer.append(event.getDelta());
                }
            }
            case "thought" -> agentStepService.recordThought(
                    event.getRunId(), assistantId, sessionId, event.getStep(), event.getText());
            case "tool_call" -> {
                long now = event.getTs() == null ? System.currentTimeMillis() : event.getTs();
                if (event.getCallId() != null) {
                    callStartedAt.put(event.getCallId(), now);
                }
                agentStepService.recordCall(event.getRunId(), assistantId, sessionId,
                        event.getCallId(), event.getStep(), event.getName(), toJson(event.getArgs()));
            }
            case "tool_result" -> {
                Long startedAt = event.getCallId() == null
                        ? null
                        : callStartedAt.remove(event.getCallId());
                // 优先用 Python 实测值：两侧时钟可能有偏差，且它不含网络分帧抖动。
                // 老版本上游没有该字段时才回退到 ts 相减。
                Integer elapsedMs = event.getElapsedMs();
                if (elapsedMs == null && startedAt != null && event.getTs() != null) {
                    elapsedMs = (int) Math.max(0, event.getTs() - startedAt);
                }
                agentStepService.recordResult(event.getRunId(), event.getCallId(),
                        Boolean.TRUE.equals(event.getOk()), event.getSummary(), elapsedMs);
            }
            case "error" -> {
                String detail = TextUtils.isBlank(event.getMessage())
                        ? event.getCode()
                        : event.getMessage();
                upstreamError.set(detail == null ? "上游 agent 出错" : detail);
            }
            case "done" -> sawDone.set(true);
            default -> {
                // start / confirm_required 等：对落库没有额外动作，原样转发即可
            }
        }
    }

    private String toJson(Map<String, Object> args) {
        if (args == null) {
            return null;
        }
        try {
            return objectMapper.writeValueAsString(args);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * 终态落库，换线程 + 吞异常（理由见 {@code RagService.settleAsync}）。
     */
    private void settleAsync(Long assistantId, StringBuilder answer, SignalType signal,
                             Throwable failure, boolean sawDone, String upstreamError) {
        ChatMsgStatus status = classify(signal, answer.length(), failure, sawDone);
        String detail = detailOf(failure, status, upstreamError);
        log.info("agent 问答终态: assistantId={} signal={} 收到={}字 sawDone={} 异常={} → 落库 {}",
                assistantId, signal, answer.length(), sawDone,
                failure == null ? "无" : failure.getClass().getName(), status);
        Schedulers.boundedElastic().schedule(() -> {
            try {
                chatRecordService.settle(assistantId, answer.toString(), status, detail);
            } catch (Exception e) {
                log.error("落库失败（agent 回答已正常返回用户，数据不会进库）: assistantId={} status={}",
                        assistantId, status, e);
            }
        });
    }

    /**
     * 流的结束方式 → 落库状态。
     *
     * <pre>
     *   CANCEL / 客户端断开          → CANCELED
     *   ON_COMPLETE + 见过 done      → DONE
     *   其它 + 一个字都没收到         → ERROR
     *   其它 + 有内容                → INTERRUPTED
     * </pre>
     *
     * <p>★ 不能把 ON_COMPLETE 直接当成功：Python 的 BUSY / MODEL_ERROR / MAX_STEPS
     * 都是「发一个 error 事件然后正常 return」。
     */
    private static ChatMsgStatus classify(SignalType signal, int textLength,
                                          Throwable failure, boolean sawDone) {
        if (signal == SignalType.CANCEL || isClientAbort(failure)) {
            return ChatMsgStatus.CANCELED;
        }
        if (signal == SignalType.ON_COMPLETE && sawDone) {
            return ChatMsgStatus.DONE;
        }
        return textLength == 0 ? ChatMsgStatus.ERROR : ChatMsgStatus.INTERRUPTED;
    }

    /**
     * 把「连不上/读不了上游」翻成 502。
     *
     * <p>否则会落进 {@code GlobalExceptionHandler} 的兜底分支，变成一句没有信息量的
     * 「服务内部错误」—— Java 明明活着，真相是它背后那一步没起来。
     * 已经带状态码的 {@link RagApiException} 原样放过。
     */
    private static Throwable translateUpstreamError(Throwable failure) {
        if (failure instanceof RagApiException) {
            return failure;
        }
        if (failure instanceof WebClientRequestException) {
            return new RagApiException(502,
                    "无法连接上游 RAG 服务（默认 8102）：" + failure.getMessage());
        }
        return failure;
    }

    private static boolean isClientAbort(Throwable failure) {
        for (Throwable t = failure; t != null; t = t.getCause()) {
            if (t.getClass().getName().contains("ClientAbort")) {
                return true;
            }
            if (t == t.getCause()) {
                break;
            }
        }
        return false;
    }

    private static String detailOf(Throwable failure, ChatMsgStatus status, String upstreamError) {
        if (status == ChatMsgStatus.DONE || status == ChatMsgStatus.CANCELED) {
            return "";
        }
        // 上游 error 事件是给用户看的说明，优先于框架的异常文案
        if (!TextUtils.isBlank(upstreamError)) {
            return TextUtils.truncate(upstreamError, AppConstants.MAX_ERROR_CHARS);
        }
        if (failure instanceof RagApiException rae) {
            return TextUtils.truncate(rae.getDetail(), AppConstants.MAX_ERROR_CHARS);
        }
        if (failure == null) {
            return "上游在没有发送 done 事件的情况下结束了本次回答";
        }
        String message = failure.getMessage();
        return TextUtils.truncate(
                failure.getClass().getSimpleName() + (message == null ? "" : ": " + message),
                AppConstants.MAX_ERROR_CHARS);
    }
}
