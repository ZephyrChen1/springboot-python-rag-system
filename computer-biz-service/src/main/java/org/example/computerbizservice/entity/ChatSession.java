package org.example.computerbizservice.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * chat_session 表。前端每次刷新页面 / 点「重新开始」都会 newSessionId()，所以会话短而多。
 *
 * <p>会话行是<b>按需创建</b>的：该 session_id 的第一次提问进来时才插一行（见 ChatRecordService
 * 的 beginExchange）。所以它不能加外键约束到别处，而 chat_message 也不给它加 ——
 * 详见 db/schema.sql 里 chat_message.session_id 的列注释。
 *
 * <p>★ 同样别加 {@code @Builder} / {@code @AllArgsConstructor}（理由见 {@link User}）。
 */
@Data
@TableName("chat_session")
public class ChatSession {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 前端生成的会话号（UUID），唯一。上游要求 1~128 位、只允许字母/数字/下划线/连字符 */
    private String sessionId;

    private Long userId;

    /** 会话标题：取第一句提问的前若干字（AppConstants.TITLE_SOURCE_CHARS），建会话时顺手填一次 */
    private String title;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;
}
