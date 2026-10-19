package org.example.computerbizservice.controller;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.client.RagClient;
import org.example.computerbizservice.dto.request.rag.AgentChatReq;
import org.example.computerbizservice.dto.request.rag.AgentConfirmReq;
import org.example.computerbizservice.dto.response.rag.RagStageResp;
import org.example.computerbizservice.service.AgentChatService;
import org.example.computerbizservice.service.RagService;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import reactor.core.publisher.Flux;

import java.util.Map;

/**
 * Agentic RAG 的两个前端入口，和旧的 /api/rag/chat 并存。
 *
 * <p>{@code /agent/chat} 把上游的 NDJSON 事件<b>原样</b>变成 SSE 帧：
 * 一个事件 = 一个 {@code data: {...}}。前端用已有的 SSE 分帧器（fetch +
 * ReadableStream）就能读，只是 data 里是 JSON 而不是纯文本。
 */
@RestController
@RequestMapping("/api/rag")
@RequiredArgsConstructor
public class AgentController {

    private final AgentChatService agentChatService;
    private final RagClient ragClient;
    private final RagService ragService;

    /** 前端提问，返回事件流（start / thought / tool_call / tool_result / answer / done / error） */
    @PostMapping(value = "/agent/chat", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<String> chat(@RequestBody AgentChatReq req) {
        return agentChatService.chat(req);
    }

    /**
     * 批准/拒绝一次等待中的写操作（kb_ingest / kb_delete）。
     *
     * <p>这是独立的一次 HTTP 调用，不占用问答那条流 —— 所以决策循环可以得到
     * {@code confirm_required} 之后继续等待，前端也能用普通按钮完成确认。
     * token 过期或不存在时上游返回 404，由 GlobalExceptionHandler 统一翻译。
     */
    /**
     * 只暂存文件，不入库。前端在 Agent 问答页上传后拿到 staged_id，
     * 真正入库与否交给对话里的 Agent（kb_ingest 会再走人工确认）。
     */
    @PostMapping(value = "/agent/stage", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public RagStageResp stage(@RequestPart("file") MultipartFile file) {
        return ragService.stage(file);
    }

    @PostMapping("/agent/confirm")
    public Map<String, Object> confirm(@RequestBody AgentConfirmReq req) {
        if (req == null || req.getToken() == null || req.getToken().isBlank()) {
            throw new IllegalArgumentException("token 不能为空");
        }
        Boolean approved = req.getApproved() == null ? Boolean.TRUE : req.getApproved();
        req.setApproved(approved);
        ragClient.agentConfirm(req);
        return Map.of("ok", true, "approved", approved);
    }
}
