package org.example.computerbizservice.controller;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.dto.request.rag.AiChatReq;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.service.RagService;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import reactor.core.publisher.Flux;

@RestController //最外层接口入口，负责接收前端发来的 HTTP 请求，把参数传给RagService，拿到结果再返回给前端
@RequestMapping("/api/rag") //统一前缀，和fastapi暴露给java的可以不一样，这是暴露给前端的
@RequiredArgsConstructor
public class RagController {

    private final RagService ragService;

    /** 前端上传文档，返回 job_id 供轮询 */
    @PostMapping(value = "/documents", consumes = MediaType.MULTIPART_FORM_DATA_VALUE) //声明接口接收 文件上传表单
    public FileUploadResp upload(@RequestPart("file") MultipartFile file,
                                 @ModelAttribute FileUploadReq extra) {
        return ragService.upload(file, extra);
    }

    /** 前端拿 job_id 查进度 */
    @GetMapping("/tasks/{jobId}")
    public TaskQueryResp queryTask(@PathVariable String jobId) { //从 URL 路径中取出 jobId
        return ragService.queryTask(jobId);
    }

    /** 前端输入问题， ai回答 */
    @PostMapping(value = "/chat", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public Flux<String> aiChat(@RequestBody AiChatReq req) {
        return ragService.aiChat(req);
    }
}