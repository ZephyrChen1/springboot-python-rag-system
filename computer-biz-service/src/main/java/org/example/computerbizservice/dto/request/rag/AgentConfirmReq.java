package org.example.computerbizservice.dto.request.rag;

import lombok.Data;

/**
 * 前端对一次写操作（如 kb_delete）的确认。
 *
 * <p>{@code token} 来自流里的 {@code confirm_required} 事件；上游按 token 找到挂起的
 * 决策循环并唤醒它。{@code approved} 缺省为 true（和上游的默认值一致）。
 */
@Data
public class AgentConfirmReq {

    private String token;

    private Boolean approved = Boolean.TRUE;
}
