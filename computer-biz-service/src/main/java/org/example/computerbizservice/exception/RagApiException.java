package org.example.computerbizservice.exception;

import lombok.Getter;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.List;

/** FastAPI返回 非2xx 时抛出，由Global转成相应 */
@Getter
public class RagApiException extends RuntimeException {

    private final int status;
    private final String detail;

    public RagApiException(int status, String detail) {
        super(detail);
        this.status = status;
        this.detail = detail;
    }//构造函数

    /**
     * 从上游的错误响应体造异常。
     *
     * ★ 两个客户端（RestClient 和 WebClient）都要走这里，**detail 只能有一份翻译实现**：
     * 各写一份的话，早晚有一条路先腐烂，而它偏偏是排查问题时唯一的线索。
     */
    public static RagApiException fromUpstream(int status, String body, ObjectMapper objectMapper) {
        return new RagApiException(status, detailOf(body, objectMapper));
    }

    /**
     * 把上游的错误体抠成一句人话。
     *
     * FastAPI 有两种错误体，都得认：
     *   1) 业务错误       {"detail":"任务不存在(仅保留最近一个任务)"}
     *   2) 参数校验失败   {"detail":[{"type":"int_type","loc":["body","user_id"],
     *                                "msg":"Input should be a valid integer","input":null}]}
     * 第二种的 detail 是**数组**，用 ApiErrorResp(String detail) 反序列化会直接抛异常。
     * 结果就是「整段原始 JSON 被当成错误文案丢给用户」—— 名字和原因都在里面，但没人愿意读。
     * 这次排查 /ai/chat 的 500 就卡在这儿，所以在这里一次性翻好。
     */
    public static String detailOf(String body, ObjectMapper objectMapper) {
        if (body == null || body.isBlank()) {
            return "上游返回空响应体";
        }
        try {
            JsonNode detail = objectMapper.readTree(body).path("detail");
            if (detail.isString()) {
                String text = detail.stringValue();
                if (!text.isBlank()) {
                    return text;
                }
            }
            if (detail.isArray()) {
                List<String> messages = new ArrayList<>();
                for (JsonNode item : detail) {
                    String msg = item.path("msg").isString() ? item.path("msg").stringValue() : null;
                    if (msg == null || msg.isBlank()) {
                        messages.add(item.toString());
                        continue;
                    }
                    // loc 形如 ["body","user_id"]，只留字段名：'user_id: Input should be a valid integer'
                    String field = lastFieldName(item.path("loc"));
                    messages.add(field == null ? msg : field + ": " + msg);
                }
                if (!messages.isEmpty()) {
                    return String.join("；", messages);
                }
            }
        } catch (Exception ignored) {
            // 上游没按约定返回 JSON（代理的 HTML 错误页等），退化成原始文本
        }
        return body;
    }

    /** loc 数组里去掉 "body"/"query" 这类位置前缀，只留最后一段字段名 */
    private static String lastFieldName(JsonNode loc) {
        if (!loc.isArray() || loc.isEmpty()) {
            return null;
        }
        JsonNode last = loc.get(loc.size() - 1);
        return last.isString() ? last.stringValue() : last.toString();
    }
}
