package org.example.computerbizservice.service;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.baomidou.mybatisplus.spring.service.impl.ServiceImpl;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.common.AppConstants;
import org.example.computerbizservice.common.enums.ChatMsgStatus;
import org.example.computerbizservice.common.enums.ChatRole;
import org.example.computerbizservice.dto.request.rag.AiChatReq;
import org.example.computerbizservice.entity.ChatMessage;
import org.example.computerbizservice.entity.ChatSession;
import org.example.computerbizservice.mapper.ChatMessageMapper;
import org.example.computerbizservice.mapper.ChatSessionMapper;
import org.example.computerbizservice.util.TextUtils;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;

/**
 * 问答记录的落库。两个方法，分别对应一次问答的<b>开头</b>和<b>终态</b>，
 * 都由 {@code RagService.aiChat} 调用。
 *
 * <p>泛型挂在 message 上（{@code ServiceImpl<ChatMessageMapper, ChatMessage>}），因为这里主要动的是消息；
 * 会话那张表另外注入一个 mapper 来用。
 *
 * <p>★ {@code ServiceImpl} 的包名是 {@code com.baomidou.mybatisplus.spring.service.impl}
 * —— 3.5.17 重构过，网上教程里的 {@code extension.service.impl} 在这个版本里是空的。
 * 可用方法没变：{@code save / updateById / getById / getOne / lambdaQuery / lambdaUpdate}
 * 都还在（实现在 {@code ...extension.repository.IRepository} 上）。
 *
 * <h2>① {@link #beginExchange} —— 提问进来、流还没开始</h2>
 * <p>在<b>请求线程</b>上同步跑（不是流的一部分），因为这时响应还没开始，写库失败还能被抓住记日志；
 * 放进 {@code Flux.defer()} 里写的话，落库就成了流的一部分 —— DB 一抖会把用户的回答带崩。
 *
 * <p>一个事务里做三件事：
 * <ol>
 *   <li>按 session_id 查 {@code chat_session}，没有就插一行（title = 提问的前
 *       {@link AppConstants#TITLE_SOURCE_CHARS} 个字）。<b>按需创建</b>：前端每次刷新页面都会
 *       {@code newSessionId()}，多余的会话行只会是垃圾。</li>
 *   <li>插 user 行：{@code role=USER}、content = 提问原文、{@code status=DONE}
 *       （提问本身没有流，写进去就是终态）、topK（可能为 null = 用上游默认值）、createdAt。</li>
 *   <li>插 assistant 行：{@code role=ASSISTANT}、{@code content=null}、{@code status=STREAMING}、
 *       createdAt（<b>和上一行同一个值</b>）、finishedAt 留 null，并把它生成的 id 返回。</li>
 * </ol>
 *
 * <p>★ <b>两行必须共用一个 {@code now}</b>：先取一次 {@code LocalDateTime} 再给两行用，
 * 不要各写一次 {@code LocalDateTime.now()}。前端的语义里这两条消息是「同一条消息对」
 * （共享 createdAt），差几毫秒就破坏了它；而且一轮之内的先后本来就要靠 id 排、不能靠时间。
 *
 * <p>为什么要事务：只有 user 行、没有 assistant 行，读起来像「模型无视了这个问题」，是假象。
 * 两行必须都在或都不在。
 *
 * <p>★ 事务边界：<b>绝不要把上游调用包进事务里</b>。这里没调上游，但别哪天顺手把
 * {@code ragClient} 调进来 —— 上传那条路可能传 100MB、问答是个可能几分钟的流，
 * 一个事务横跨它们会长时间占住连接池里的连接。写库与调用要严格前后排列，不嵌套。
 *
 * <p>请求不合法（{@code req} 为 null、sessionId 空、message 空）时<b>返回 null 且什么都不写</b>：
 * 上游反正会 422 拒掉这次请求，留下一行「用户说了话、模型没理他」的记录比不留还坏。
 * {@code RagService} 拿到 null 就整条 tee 跳过，问答照常。
 *
 * <h2>② {@link #settle} —— 流的终态</h2>
 * <p>单条 UPDATE，只写这几列：content、charCount、status、error、finishedAt。
 * 用 {@code updateById(补丁对象)}：补丁里为 null 的字段<b>不会</b>进 SET 子句，
 * 所以不会碰到这个类没打算改的列（比如 topK）。
 *
 * <p>★ error 必须是空串而不是 null：正常结束/被取消时如果传了 null，MP 会把它整列跳过，
 * 上一次残留的报错就会永远挂在这行上（清空只能靠空串）。终态文案由
 * {@code RagService.detailOf} 负责算。
 *
 * <p>★ <b>取消时 content 里有半截回答 —— 这是这个设计最重要的产物</b>：
 * 用户点「停止生成」之后，前端刷新就丢的东西，只有这里留得住。
 *
 * <p>不做「只能改 STREAMING 行」的条件更新（{@code ingest_task} 那边做了）：那边两个页签
 * 会并发轮询同一个 job，必须靠 WHERE 兜住；这边一次问答只有一条流、而且
 * {@code RagService} 用 {@code AtomicBoolean} 保证了终态只落一次，没有竞态要防。
 *
 * <p>调用方把它丢在弹性线程池上、并且整体 try/catch —— 落库失败只该变成一行日志，
 * 不该把已经返回给用户的回答变成报错。所以<b>这个方法不自己吞异常</b>，
 * 出问题让调用方那层去记（它才有 assistantId 这个上下文）。
 *
 * <h2>自己怎么验</h2>
 * <pre>
 * SELECT id, session_id, role, status, CHAR_LENGTH(content) len, char_count,
 *        LEFT(content,30) head, error, top_k, created_at, finished_at
 * FROM chat_message ORDER BY id DESC LIMIT 10;
 * </pre>
 * 一问一答两行、created_at 相同；正常答完 assistant 行是 DONE 且有正文；
 * 中途点「停止生成」→ CANCELED + 半截正文 + error 为空串；把 FastAPI 停掉再问 → ERROR 且正文为空；
 * 答到一半把 FastAPI 停掉 → INTERRUPTED + 有正文。
 * 另外这条应该是空的（有货就说明有流卡住了）：<br>
 * {@code SELECT * FROM chat_message WHERE status='STREAMING' AND finished_at IS NULL;}
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class ChatRecordService extends ServiceImpl<ChatMessageMapper, ChatMessage> {

    /** 会话表不是这张 service 的泛型主角，单独注入一个用它 */
    private final ChatSessionMapper chatSessionMapper;

    /**
     * ① 开一轮问答：补会话行 + 插 user 行 + 插一行空的 assistant 行。
     *
     * @param userId 本轮恒为 {@link AppConstants#DEFAULT_USER_ID}（由 {@code RagService} 传进来）。
     *               ★ 不要改成读 {@code req.getUserId()} —— 没有鉴权的现在，客户端传什么就信什么是白送的越权
     * @return assistant 行的 id（终态时 {@link #settle} 靠它定位）；请求不合法时返回 null
     */
    @Transactional
    public Long beginExchange(AiChatReq req, long userId) {
        if (req == null) {
            return null;
        }
        return doBeginExchange(req.getSessionId(), req.getMessage(), userId, req.getTopK());
    }

    /**
     * Agent 问答用的重载：请求体里没有 topK（/ai/agent/chat 不接收这个参数）。
     *
     * <p>两个 public 重载都带 {@code @Transactional}，因为 Spring 的事务是代理织入的：
     * 从外部调用哪个方法，哪个方法上的注解才生效。私有核心方法不注解，避免自调用陷阱。
     */
    @Transactional
    public Long beginExchange(String sessionId, String message, long userId) {
        return doBeginExchange(sessionId, message, userId, null);
    }

    private Long doBeginExchange(String sessionId, String message, long userId, Integer topK) {
        if (TextUtils.isBlank(sessionId) || TextUtils.isBlank(message)) {
            log.debug("问答落库：请求不合法（sessionId/message 为空），不写任何行（上游也会 422 拒掉它）");
            return null;
        }

        ensureSession(sessionId, userId, message);

        // ★ 两行共用一个时间戳：前端把那两条消息当作「一条消息对」（见类注释）
        LocalDateTime now = LocalDateTime.now();

        ChatMessage question = new ChatMessage();
        question.setSessionId(sessionId);
        question.setUserId(userId);
        question.setRole(ChatRole.USER);
        question.setContent(message);
        question.setStatus(ChatMsgStatus.DONE); // 提问本身没有流，写进去就是终态
        question.setTopK(topK);                 // 可能为 null，代表「用上游默认值」
        question.setCreatedAt(now);
        save(question);

        ChatMessage answer = new ChatMessage();
        answer.setSessionId(sessionId);
        answer.setUserId(userId);
        answer.setRole(ChatRole.ASSISTANT);
        answer.setStatus(ChatMsgStatus.STREAMING);
        answer.setCreatedAt(now); // ← 同一个 now
        // content / error / charCount / finishedAt 留 null：还没生成，也就还没有终态

        save(answer); // 自增主键回填进 answer.getId()
        log.debug("问答落库：session={} 已建 user 行 id={} + assistant 行 id={}（status=STREAMING）",
                sessionId, question.getId(), answer.getId());
        return answer.getId();
    }

    /**
     * ② 流结束（正常/出错/被取消）时写终态。
     *
     * @param answer 攒到的正文，可能只有半截，也可能是空串（一个字都没收到）
     * @param status 由 {@code RagService.classify} 判出来，这里的值必须已经和前端那套语义对齐
     * @param error  终态原因；正常结束/被取消时是空串（★ 不是 null，见类注释）
     */
    public void settle(Long assistantId, String answer, ChatMsgStatus status, String error) {
        if (assistantId == null) {
            // 正常流程走不到（RagService 在 beginExchange 返回 null 时整条跳过），留着是为了让这个方法自成一体
            log.debug("问答终态落库：assistantId 为空，跳过");
            return;
        }

        String text = answer == null ? "" : answer;
        ChatMessage patch = new ChatMessage();
        patch.setId(assistantId);
        patch.setContent(text);
        patch.setCharCount(text.length()); // 一眼看出哪些回答是半截的
        patch.setStatus(status);
        patch.setError(error == null ? "" : error); // ★ 空串才能清掉旧值，null 会被 MP 跳过
        patch.setFinishedAt(LocalDateTime.now());
        // 其余字段留 null → 不进 SET 子句，所以 topK / created_at / session_id 都不会被动

        boolean updated = updateById(patch);
        log.debug("问答终态落库：assistantId={} status={} {} 字 → {}", assistantId, status, text.length(),
                updated ? "已写入" : "未命中任何行（行被删了？）");
    }

    /**
     * 会话行按需创建：这个 session_id 的第一次提问进来时插一行。
     *
     * <p>{@code title} 只在这里填一次，之后不再维护 —— 它是列表页的展示用字段，
     * 用第一句话当标题是最省事的近似；真要做「自动起标题」是另一件事，那时会多一次上游调用。
     *
     * <p>{@code selectOne}：session_id 上有唯一键，所以最多一行（多于一行说明索引被改了，那时候该炸）。
     */
    private void ensureSession(String sessionId, long userId, String firstMessage) {
        ChatSession existing = chatSessionMapper.selectOne(
                Wrappers.<ChatSession>lambdaQuery().eq(ChatSession::getSessionId, sessionId));
        if (existing != null) {
            return;
        }
        ChatSession session = new ChatSession();
        session.setSessionId(sessionId); // ★ 不截断：业务键，超长就让它报错（理由见 AppConstants）
        session.setUserId(userId);
        session.setTitle(TextUtils.truncate(firstMessage, AppConstants.TITLE_SOURCE_CHARS));
        // created_at / updated_at 交给数据库默认值
        chatSessionMapper.insert(session);
        log.debug("问答落库：新会话 session={} id={} title={}", sessionId, session.getId(), session.getTitle());
    }
}
