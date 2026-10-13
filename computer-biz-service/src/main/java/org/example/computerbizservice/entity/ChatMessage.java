package org.example.computerbizservice.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import org.example.computerbizservice.common.enums.ChatMsgStatus;
import org.example.computerbizservice.common.enums.ChatRole;

import java.time.LocalDateTime;

/**
 * chat_message 表：一次问答两行（user + assistant）。
 *
 * <p>助手那行的正文是 Java 代理 SSE 时<b>自己攒</b>的 —— 上游只会把答案流给我们，
 * 不会在别处留一份。所以这行的 content 是「本来会随进程消失」的东西，落库的价值最大。
 *
 * <p>★ 同样别加 {@code @Builder} / {@code @AllArgsConstructor}（理由见 {@link User}）。
 */
@Data
@TableName("chat_message")
public class ChatMessage {

    @TableId(type = IdType.AUTO)
    private Long id;

    /**
     * 指向 chat_session.session_id。★ 一轮问答的两行（user/assistant）用<b>同一个</b>
     * sessionId，所以一个 session 有很多行 —— 这就是它在这张表上<b>不唯一</b>、
     * 唯一键只能建在 chat_session 上的原因。
     */
    private String sessionId;

    private Long userId;

    private ChatRole role;

    /**
     * user 行 = 提问原文；assistant 行 = 已收到的回答（可能只有半截，可能还没开始）。
     *
     * <p>★ 存的是<b>上游原始流文本</b>，和用户屏幕上看到的未必一致：前端会丢掉它不认识的
     * JSON 帧（{@code {"sources":[...]}} 之类）。在 Java 侧再实现一遍那个过滤 = 制造第二份
     * 前端契约，这正是本项目一贯拒绝的事（错误翻译「只能有一份实现」同理）。
     */
    private String content;

    /** 见 {@link ChatMsgStatus}。终态在流结束时写一次；助手行是先以 STREAMING 建出来的 */
    private ChatMsgStatus status;

    /** ERROR / INTERRUPTED 时的原因；DONE 和 CANCELED 时是空串（不是 null，理由见 {@link IngestTask} 的 error 字段） */
    private String error;

    /** 本次检索条数，只写在 user 行。可能为 null（= 用上游默认值） */
    private Integer topK;

    /** 助手正文的字符数。一眼看出哪些回答是半截的 */
    private Integer charCount;

    /**
     * ★ 同一轮问答的 user / assistant 两行用<b>同一个值</b>（对齐前端「两条消息共享 createdAt」
     * 的语义）。所以一轮之内的先后顺序只能靠 id 排，不能靠时间。
     */
    private LocalDateTime createdAt;

    /** 终态时间。STREAMING 时是 null —— 用它就能捞「卡住的流」 */
    private LocalDateTime finishedAt;
}
