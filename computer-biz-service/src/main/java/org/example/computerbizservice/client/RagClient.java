package org.example.computerbizservice.client;

import lombok.RequiredArgsConstructor;

import org.example.computerbizservice.dto.request.rag.AgentChatReq;
import org.example.computerbizservice.dto.request.rag.AgentConfirmReq;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.NewsItemPageResp;
import org.example.computerbizservice.dto.response.rag.NewsItemResp;
import org.example.computerbizservice.dto.response.rag.NewsOverviewResp;
import org.example.computerbizservice.dto.response.rag.RagStageResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.dto.request.rag.AiChatReq;
import org.example.computerbizservice.exception.RagApiException;

import org.springframework.core.io.Resource;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.http.MediaType;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.reactive.function.client.WebClient; //处理SSE
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.databind.ObjectMapper;

import java.util.Optional;

import reactor.core.publisher.Flux;

/**
 * 对 FastAPI rag 服务的调用层：一个方法对应一个上游接口，只做「拼请求 + 反序列化」。
 * 业务规则（重试、轮询、落库）放 RagService，不要写在这里。
 */
@Component //变成Bean，供后续业务service类使用
@RequiredArgsConstructor //对所有final成员变量生成构造函数
public class RagClient {

    private static final String UPLOAD_PATH = "/v1/kb/upload";
    private static final String STAGE_PATH = "/v1/kb/stage";
    private static final String INGEST_PATH = "/v1/kb/ingest";
    private static final String CHAT_PATH = "/ai/chat";
    private static final String AGENT_CHAT_PATH = "/ai/agent/chat";
    private static final String AGENT_CONFIRM_PATH = "/ai/agent/confirm";
    private static final String NEWS_OVERVIEW_PATH = "/v1/news/overview";
    private static final String NEWS_ITEMS_PATH = "/v1/news/items";

    private final RestClient ragRestClient;
    private final WebClient ragWebClient; //专门处理SSE
    private final ObjectMapper objectMapper; // 只为把上游的错误体翻译成 RagApiException.detail

    /** POST /v1/kb/ingest，提交文档入库，立即返回 job_id */
    public FileUploadResp upload(MultipartFile file, FileUploadReq extra) {
        MultipartBodyBuilder body = new MultipartBodyBuilder(); //构建文件上传表单
        body.part("file", toResource(file))
                .filename(Optional.ofNullable(file.getOriginalFilename()).orElse("unknown"))
                .contentType(parseMediaType(file.getContentType())); //往表单注入文件

        return ragRestClient.post() //发送POST请求
                .uri(UPLOAD_PATH)
                .contentType(MediaType.MULTIPART_FORM_DATA) //表明这是文件上传表单
                .body(body.build()) //发给FastAPI
                .retrieve() //等待获取响应
                .body(FileUploadResp.class); //接收FastAPI返回
    }

    /**
     * POST /v1/kb/stage：只暂存文件，立即返回 staged_id。
     *
     * <p>和 {@link #upload} 的区别是「入库由谁决定」：upload 直接开始入库，
     * stage 只落盘，等 Agent 在对话里判断用户确实要入库时再调 kb_ingest。
     */
    public RagStageResp stage(MultipartFile file) {
        MultipartBodyBuilder body = new MultipartBodyBuilder();
        body.part("file", toResource(file))
                .filename(Optional.ofNullable(file.getOriginalFilename()).orElse("unknown"))
                .contentType(parseMediaType(file.getContentType()));

        return ragRestClient.post()
                .uri(STAGE_PATH)
                .contentType(MediaType.MULTIPART_FORM_DATA)
                .body(body.build())
                .retrieve()
                .body(RagStageResp.class);
    }

    /** GET /v1/kb/ingest/{job_id}，查询入库任务进度 */
    public TaskQueryResp queryTask(String jobId) {
        return ragRestClient.get()
                .uri(INGEST_PATH + "/{jobId}", jobId)
                .retrieve()
                .body(TaskQueryResp.class);
    }

    // ------------------------------------------------------------ 抓取动态

    /** GET /v1/news/overview，抓取统计、源状态和最近运行记录（只读） */
    public NewsOverviewResp newsOverview(int limit) {
        return ragRestClient.get()
                .uri(builder -> builder.path(NEWS_OVERVIEW_PATH)
                        .queryParam("limit", limit)
                        .build())
                .retrieve()
                .body(NewsOverviewResp.class);
    }

    /** GET /v1/news/items，抓取文章分页列表（只读） */
    public NewsItemPageResp newsItems(String status, String sourceId, String q, int page, int pageSize) {
        return ragRestClient.get()
                .uri(builder -> {
                    builder.path(NEWS_ITEMS_PATH)
                            .queryParam("page", page)
                            .queryParam("page_size", pageSize);
                    if (status != null && !status.isBlank()) {
                        builder.queryParam("status", status);
                    }
                    if (sourceId != null && !sourceId.isBlank()) {
                        builder.queryParam("source_id", sourceId);
                    }
                    if (q != null && !q.isBlank()) {
                        builder.queryParam("q", q);
                    }
                    return builder.build();
                })
                .retrieve()
                .body(NewsItemPageResp.class);
    }

    /** GET /v1/news/items/{id}，单篇文章详情（含摘要、关键事实和正文） */
    public NewsItemResp newsItem(long id) {
        return ragRestClient.get()
                .uri(NEWS_ITEMS_PATH + "/{id}", id)
                .retrieve()
                .body(NewsItemResp.class);
    }

    /** POST /ai/chat, ai问答 */
    public Flux<String> aiChat(AiChatReq req) {
        return ragWebClient.post()
                .uri(CHAT_PATH)
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM) // 关键头，协商SSE
                .bodyValue(req)
                // 用 exchangeToFlux 而不是 retrieve()：流式响应要能在「同一个响应对象上」
                // 先看状态码、再决定是读错误体还是读正文，retrieve() 做不到
                .exchangeToFlux(response -> {
                    if (response.statusCode().isError()) {
                        return response.bodyToMono(String.class)
                                .defaultIfEmpty("")
                                .flatMapMany(body -> Flux.error(RagApiException.fromUpstream(
                                        response.statusCode().value(), body, objectMapper)));
                    }
                    return decodeRawText(response.bodyToFlux(DataBuffer.class));
                });
    }

    /**
     * POST /ai/agent/chat，Agentic RAG 问答，逐行返回 NDJSON 事件。
     *
     * <p>和 {@link #aiChat} 的三个不同点：
     * <ul>
     *   <li><b>身份走请求头</b>：{@code X-User-Id} 由 Java 注入（本轮恒为演示用户），
     *       请求体里没有 user_id —— 不信客户端声明的身份。</li>
     *   <li><b>响应是 {@code application/x-ndjson}</b>：一行一个事件 JSON。仍然走
     *       {@link Utf8Stream#lines} 自己按字节解，不交给 Spring 的 NDJSON/SSE 读取器
     *       （理由同 {@link #decodeRawText}：读取器会按响应头替我们解释内容）。</li>
     *   <li>错误处理与 {@link #aiChat} 完全一致：开流前失败翻成 {@link RagApiException}。</li>
     * </ul>
     *
     * @param userId 由服务端决定，绝不取自请求体
     */
    public Flux<String> agentChat(AgentChatReq req, long userId) {
        return ragWebClient.post()
                .uri(AGENT_CHAT_PATH)
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.APPLICATION_NDJSON)
                .header("X-User-Id", Long.toString(userId))
                .bodyValue(req)
                .exchangeToFlux(response -> {
                    if (response.statusCode().isError()) {
                        return response.bodyToMono(String.class)
                                .defaultIfEmpty("")
                                .flatMapMany(body -> Flux.error(RagApiException.fromUpstream(
                                        response.statusCode().value(), body, objectMapper)));
                    }
                    return Utf8Stream.lines(response.bodyToFlux(DataBuffer.class));
                });
    }

    /**
     * POST /ai/agent/confirm，批准/拒绝一次等待中的写操作。
     *
     * <p>同步调用：确认接口本身不流式，且前端需要立刻知道 token 是否还有效
     * （过期/不存在时上游返回 404，由 {@code RagApiException} 翻译）。
     */
    public void agentConfirm(AgentConfirmReq req) {
        ragRestClient.post()
                .uri(AGENT_CONFIRM_PATH)
                .contentType(MediaType.APPLICATION_JSON)
                .body(req) // RestClient 是 body(...)；bodyValue(...) 是 WebClient 的 API
                .retrieve()
                .toBodilessEntity();
    }

    /**
     * 把上游的响应体按**原始字节**解成文本块。
     *
     * ★ 为什么不能直接 .bodyToFlux(String.class)：
     * 上游 /ai/chat 声明了 Content-Type: text/event-stream，但吐出来的是**裸文本，
     * 没有 `data:` 分帧**（od 看过字节，直接就是 UTF-8 的中文）。而 Spring 看到
     * text/event-stream 会选 ServerSentEventHttpMessageReader 去解析「SSE 事件」，
     * 按规范把不带 data:/event:/id:/retry: 前缀的行当**未知字段直接丢掉** ——
     * 于是解析出 0 个事件，整条回答静默消失：接口返回 200、流是空的、前端白等。
     * 所以这里绕开编解码器，自己拿 DataBuffer 读字节。
     *
     * 编解码器的选择是**按响应头的 Content-Type** 定的，请求头 Accept 改不了它，
     * 所以只能从这一层绕。
     *
     * 顺带必须自己扛住的坑：块边界会切开一个汉字（UTF-8 里汉字 3 字节）。
     * 每块各自 new String(bytes) 会得到乱码，所以自己维护一个残字节缓冲（carry），
     * 并配合 CharsetDecoder 的<b>三参重载 + endOfInput=false</b> 把解不完的字节留下来 ——
     * 用成单参重载就保不住（原因见下面 decodeRawText 里那段注释）。
     * 这和前端 TextDecoder({stream:true}) 是同一件事。
     */
    private Flux<String> decodeRawText(Flux<DataBuffer> chunks) {
        // 解码细节（残字节 carry、三参 decode、DataBuffer release）集中在 Utf8Stream，
        // agent 的 NDJSON 分帧和这里的裸文本共用同一份实现 —— 那份坑只踩一次。
        return Utf8Stream.decode(chunks);
    }

    private Resource toResource(MultipartFile file) {
        return file.getResource();
    } //MultipartFile是 Spring 接收前端上传文件的对象；file.getResource()把它转成 Spring 的 Resource，用来放进 Multipart 表单上传

    private MediaType parseMediaType(String contentType) {
        return contentType == null
                ? MediaType.APPLICATION_OCTET_STREAM
                : MediaType.parseMediaType(contentType);
    }
}