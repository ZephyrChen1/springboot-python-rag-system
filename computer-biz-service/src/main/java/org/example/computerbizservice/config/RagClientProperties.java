package org.example.computerbizservice.config;

import lombok.Data; //提供如setter、getter方法,比如RagClientConfig里的getBaseUrl可以直接拿到baseUrl
import org.springframework.boot.context.properties.ConfigurationProperties; //只读yml
import org.springframework.stereotype.Component; //变成Bean，后续直接在 Service/Config 里`@Autowired`注入这个类使用

/** FastAPI 侧 rag 服务的连接配置，对应 application.yaml 里的 rag. */
@Data
@Component
@ConfigurationProperties(prefix = "rag")
public class RagClientProperties {
    /** 根地址，不要以 / 结尾 */
    private String baseUrl = "http://localhost:8102";

    /**
     * Python agent 的 biz_query 工具调用 {@code /internal/rag/query} 时带的 X-API-Key。
     *
     * <p>空 = 内部查询接口整体关闭（返回 503）。对应的 Python 侧配置是
     * {@code AGENT_BIZ_QUERY_KEY}，两边必须是同一个值。
     * ★ 它不是前端用的密钥，不要下发到浏览器。
     */
    private String internalQueryKey = "";
}