package org.example.computerbizservice.dto.request.rag;

import lombok.Data;

import java.util.Map;

/**
 * Python agent 的 {@code biz_query} 工具 → Java 内部查询接口的请求体。
 *
 * <p>{@code query} 只接受白名单里的名字（见 {@code InternalRagQueryController}），
 * {@code params} 只允许该查询声明过的键。这里<b>不是</b> SQL：不接受语句、表名或排序字段。
 */
@Data
public class InternalQueryReq {

    /** user_sessions / session_messages / ingest_tasks */
    private String query;

    private Map<String, Object> params;
}
