package org.example.computerbizservice.controller;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.config.RagClientProperties;
import org.example.computerbizservice.dto.request.rag.InternalQueryReq;
import org.example.computerbizservice.dto.response.rag.InternalQueryResp;
import org.example.computerbizservice.entity.ChatMessage;
import org.example.computerbizservice.entity.ChatSession;
import org.example.computerbizservice.entity.IngestTask;
import org.example.computerbizservice.mapper.ChatMessageMapper;
import org.example.computerbizservice.mapper.ChatSessionMapper;
import org.example.computerbizservice.mapper.IngestTaskMapper;
import org.example.computerbizservice.util.TextUtils;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Python agent 的 {@code biz_query} 工具调用的内部查询接口。
 *
 * <h2>为什么不是「执行 SQL」</h2>
 * <p>让模型生成 SQL 等于把整个库的读写权限交给一段不可审计的文本。这里只提供
 * <b>三条固定的、参数化的查询</b>，每条都有：
 * <ul>
 *   <li>固定列（不返回 password_hash 这类敏感列）；</li>
 *   <li>固定排序和强制 {@code LIMIT}（1~100，服务端夹紧）；</li>
 *   <li>参数白名单（未知 query / 未知参数一律 400，而不是「忽略掉继续查」）。</li>
 * </ul>
 *
 * <h2>鉴权</h2>
 * <p>独立的 {@code X-API-Key}，来自 {@code AGENT_BIZ_QUERY_KEY}，<b>不是</b>给前端用的密钥。
 * 没配置时整个接口返回 503（默认关闭），配置了也只用常量时间比较。
 * 这个接口只该在受控网络里可达；如果将来 Java 前面有公网网关，记得把
 * {@code /internal/**} 排除掉。
 */
@RestController
@RequestMapping("/internal/rag")
@RequiredArgsConstructor
@Slf4j
public class InternalRagQueryController {

    private static final int DEFAULT_LIMIT = 20;
    private static final int MAX_LIMIT = 100;

    private final ChatSessionMapper chatSessionMapper;
    private final ChatMessageMapper chatMessageMapper;
    private final IngestTaskMapper ingestTaskMapper;
    private final RagClientProperties properties;

    /** @return 查询结果；参数不合法一律 400，不猜测调用方意图 */
    @PostMapping("/query")
    public InternalQueryResp query(
            @RequestHeader(value = "X-API-Key", required = false) String apiKey,
            @RequestBody InternalQueryReq req) {
        requireAuthorized(apiKey);
        if (req == null || TextUtils.isBlank(req.getQuery())) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "query 不能为空");
        }
        Map<String, Object> params = req.getParams() == null ? Map.of() : req.getParams();

        List<?> items = switch (req.getQuery()) {
            case "user_sessions" -> {
                requireOnly(params, Set.of("user_id", "limit"));
                long userId = requireLong(params, "user_id");
                int limit = limitOf(params);
                yield chatSessionMapper.selectList(Wrappers.<ChatSession>lambdaQuery()
                        .eq(ChatSession::getUserId, userId)
                        .orderByDesc(ChatSession::getUpdatedAt)
                        .last("LIMIT " + limit)); // limit 已夹在 1..100，且是 int
            }
            case "session_messages" -> {
                requireOnly(params, Set.of("user_id", "session_id", "limit"));
                long userId = requireLong(params, "user_id");
                String sessionId = requireString(params, "session_id");
                int limit = limitOf(params);
                List<ChatMessage> rows = chatMessageMapper.selectList(Wrappers.<ChatMessage>lambdaQuery()
                        .eq(ChatMessage::getSessionId, sessionId)
                        .eq(ChatMessage::getUserId, userId) // 纵深防御：session_id 泄漏也跨不了用户
                        .orderByDesc(ChatMessage::getId)     // 先取最近 N 条
                        .last("LIMIT " + limit));
                Collections.reverse(rows); // 再按时间正序返回，读起来像对话
                yield rows;
            }
            case "ingest_tasks" -> {
                requireOnly(params, Set.of("user_id", "limit"));
                long userId = requireLong(params, "user_id");
                int limit = limitOf(params);
                yield ingestTaskMapper.selectList(Wrappers.<IngestTask>lambdaQuery()
                        .eq(IngestTask::getUserId, userId)
                        .orderByDesc(IngestTask::getId)
                        .last("LIMIT " + limit));
            }
            default -> throw new ResponseStatusException(
                    HttpStatus.BAD_REQUEST, "不支持的 query：" + req.getQuery());
        };

        log.debug("内部查询: query={} params={} → {} 行", req.getQuery(), params, items.size());
        return new InternalQueryResp(req.getQuery(), items.size(), items);
    }

    private void requireAuthorized(String apiKey) {
        String expected = properties.getInternalQueryKey();
        if (TextUtils.isBlank(expected)) {
            // 默认关闭：没配密钥时不要让这个接口变成「谁都能查库」的后门
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                    "内部查询未启用：请设置 AGENT_BIZ_QUERY_KEY");
        }
        boolean ok = !TextUtils.isBlank(apiKey) && MessageDigest.isEqual(
                expected.getBytes(StandardCharsets.UTF_8),
                apiKey.getBytes(StandardCharsets.UTF_8)); // 常量时间比较，避免计时侧信道
        if (!ok) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN, "X-API-Key 无效");
        }
    }

    private static void requireOnly(Map<String, Object> params, Set<String> allowed) {
        for (String key : params.keySet()) {
            if (!allowed.contains(key)) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "不支持的参数：" + key);
            }
        }
    }

    private static String requireString(Map<String, Object> params, String key) {
        Object raw = params.get(key);
        if (raw instanceof String s && !s.isBlank()) {
            return s;
        }
        throw new ResponseStatusException(HttpStatus.BAD_REQUEST, key + " 不能为空");
    }

    private static long requireLong(Map<String, Object> params, String key) {
        Object raw = params.get(key);
        if (raw instanceof Number n) {
            return n.longValue();
        }
        if (raw instanceof String s) {
            try {
                return Long.parseLong(s.trim());
            } catch (NumberFormatException ignored) {
                // 落到下面的统一错误
            }
        }
        throw new ResponseStatusException(HttpStatus.BAD_REQUEST, key + " 必须是整数");
    }

    /** limit 缺省 20；非法值不报错，夹紧到 1..100（它只影响返回行数，不改变语义） */
    private static int limitOf(Map<String, Object> params) {
        Object raw = params.get("limit");
        int value = DEFAULT_LIMIT;
        if (raw instanceof Number n) {
            value = n.intValue();
        } else if (raw instanceof String s) {
            try {
                value = Integer.parseInt(s.trim());
            } catch (NumberFormatException ignored) {
                // 用默认值
            }
        }
        return Math.max(1, Math.min(value, MAX_LIMIT));
    }
}
