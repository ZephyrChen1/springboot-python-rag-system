//配置一个可直接调用 RAG FastAPI 服务的 RestClient HTTP 客户端 Bean
package org.example.computerbizservice.config;

import org.springframework.web.reactive.function.client.WebClient;
import tools.jackson.databind.ObjectMapper; //核心json工具类，让java对象和json转换
import org.example.computerbizservice.exception.RagApiException;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.client.ClientHttpResponse;
import org.springframework.web.client.RestClient;

import java.io.IOException;
import java.nio.charset.StandardCharsets; //标准字符集常量

@Configuration
public class RagClientConfig {

    @Bean
    public RestClient ragRestClient(RestClient.Builder builder,
                                    RagClientProperties properties,
                                    ObjectMapper objectMapper) {
        return builder
                .baseUrl(properties.getBaseUrl())
                // FastAPI 的错误体统一是 {"detail": "..."}，在这里一次性翻译成异常，
                // 上层就不用每处都判断状态码了
                .defaultStatusHandler(HttpStatusCode::isError, (request, response) -> {
                    throw RagApiException.fromUpstream(
                            response.getStatusCode().value(), readBody(response), objectMapper);
                })
                .build();
    } //已经配置完成的 RestClient 实例对象

    /**
     * 问答专用。
     *
     * 注意：这个 bean 上**没有** defaultStatusHandler —— 流式请求不能用它。
     * defaultStatusHandler 只在「响应头刚到、body 还没读」时抛异常，对流式响应
     * 会把连接直接掐掉；而且它拿不到 exchangeToFlux 里的上下文。所以 SSE 那条路
     * 的错误映射写在 RagClient.aiChat 的 exchangeToFlux 里，复用同一个
     * RagApiException.fromUpstream，保证两条路的 detail 翻译是同一份。
     */
    @Bean
    public WebClient ragWebClient(WebClient.Builder builder, RagClientProperties properties) {
        return builder
                .baseUrl(properties.getBaseUrl())
                .build();
    }

    private static String readBody(ClientHttpResponse response) throws IOException {
        return new String(response.getBody().readAllBytes(), StandardCharsets.UTF_8);
    }
}
