package org.example.computerbizservice.service;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import jakarta.annotation.PreDestroy;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.entity.ChatStep;
import org.example.computerbizservice.mapper.ChatStepMapper;
import org.example.computerbizservice.util.TextUtils;
import org.springframework.stereotype.Service;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

/**
 * agent 决策轨迹的落库（chat_step）。
 *
 * <h2>为什么所有写入都排在同一条线程上</h2>
 * <p>{@code tool_call} 先 INSERT、{@code tool_result} 再 UPDATE，两步来自同一个流的
 * 相邻事件。如果把它们丢进普通线程池，结果完全可能先于插入执行 —— UPDATE 命中 0 行，
 * 轨迹里就永远少一条。单线程执行器既保证顺序，又把阻塞的 JDBC 从
 * reactor-netty 事件循环上挪走（在事件循环里做 JDBC 会卡住所有请求共用的 IO 线程）。
 *
 * <p>★ 落库是旁路：任何一步失败都只打日志，绝不能让异常回到流里。前端看到的事件
 * 由 {@code AgentChatService} 原样转发，和这张表没有关系。
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class AgentStepService {

    /** args_json / result_summary 是 TEXT，给一个远小于列上限的字数，防止异常大的参数撑爆写入 */
    private static final int MAX_STORED_CHARS = 8_000;

    private final ChatStepMapper chatStepMapper;

    private final ExecutorService writer = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "agent-step-writer");
        t.setDaemon(true); // 不阻止 JVM 退出
        return t;
    });

    /** {@code thought} 事件：模型在调用工具前说的话 */
    public void recordThought(String runId, Long assistantId, String sessionId,
                              Integer stepNo, String text) {
        if (TextUtils.isBlank(runId)) {
            return;
        }
        ChatStep row = baseRow(runId, assistantId, sessionId, stepNo, "thought");
        row.setStatus("THOUGHT");
        row.setResultSummary(TextUtils.truncate(text, MAX_STORED_CHARS));
        submit("thought", runId, () -> chatStepMapper.insert(row));
    }

    /** {@code tool_call} 事件：先落一行 RUNNING，result 到达后回填 */
    public void recordCall(String runId, Long assistantId, String sessionId, String callId,
                           Integer stepNo, String name, String argsJson) {
        if (TextUtils.isBlank(runId) || TextUtils.isBlank(callId)) {
            return;
        }
        ChatStep row = baseRow(runId, assistantId, sessionId, stepNo, "tool_call");
        row.setCallId(callId);
        row.setName(TextUtils.truncate(name, 64));
        row.setArgsJson(TextUtils.truncate(argsJson, MAX_STORED_CHARS));
        row.setStatus("RUNNING");
        submit("tool_call " + callId, runId, () -> chatStepMapper.insert(row));
    }

    /** {@code tool_result} 事件：按 run_id + call_id 回填状态、摘要和耗时 */
    public void recordResult(String runId, String callId, boolean ok,
                             String summary, Integer elapsedMs) {
        if (TextUtils.isBlank(runId) || TextUtils.isBlank(callId)) {
            return;
        }
        String status = ok ? "OK" : "FAILED";
        String text = TextUtils.truncate(summary, MAX_STORED_CHARS);
        submit("tool_result " + callId, runId, () -> chatStepMapper.update(null,
                Wrappers.<ChatStep>lambdaUpdate()
                        .eq(ChatStep::getRunId, runId)
                        .eq(ChatStep::getCallId, callId)
                        .set(ChatStep::getStatus, status)
                        .set(ChatStep::getResultSummary, text)
                        .set(ChatStep::getElapsedMs, elapsedMs)));
    }

    private static ChatStep baseRow(String runId, Long assistantId, String sessionId,
                                    Integer stepNo, String type) {
        ChatStep row = new ChatStep();
        row.setRunId(runId);
        row.setAssistantId(assistantId);
        row.setSessionId(sessionId);
        row.setStepNo(stepNo);
        row.setType(type);
        // createdAt 留 null：交给数据库 DEFAULT CURRENT_TIMESTAMP(3)
        return row;
    }

    private void submit(String what, String runId, Runnable task) {
        try {
            writer.execute(() -> {
                try {
                    task.run();
                } catch (Exception e) {
                    // 轨迹写入失败只配一行日志 —— 用户正在等回答
                    log.warn("agent 轨迹落库失败（{}）run_id={}：{}", what, runId, e.toString());
                }
            });
        } catch (RejectedExecutionException e) {
            log.warn("agent 轨迹落库队列已关闭（{}）run_id={}", what, runId);
        }
    }

    @PreDestroy
    public void shutdown() {
        writer.shutdown();
    }
}
