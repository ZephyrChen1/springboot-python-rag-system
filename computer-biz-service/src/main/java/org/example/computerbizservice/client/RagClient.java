package org.example.computerbizservice.client;

import lombok.RequiredArgsConstructor;

import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.dto.request.rag.AiChatReq;
import org.example.computerbizservice.exception.RagApiException;

import org.springframework.core.io.Resource;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferUtils;
import org.springframework.http.MediaType;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.reactive.function.client.WebClient; //处理SSE
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.databind.ObjectMapper;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CoderResult;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Optional;

import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

/**
 * 对 FastAPI rag 服务的调用层：一个方法对应一个上游接口，只做「拼请求 + 反序列化」。
 * 业务规则（重试、轮询、落库）放 RagService，不要写在这里。
 */
@Component //变成Bean，供后续业务service类使用
@RequiredArgsConstructor //对所有final成员变量生成构造函数
public class RagClient {

    private static final String UPLOAD_PATH = "/v1/kb/upload";
    private static final String INGEST_PATH = "/v1/kb/ingest";
    private static final String CHAT_PATH = "/ai/chat";

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

    /** GET /v1/kb/ingest/{job_id}，查询入库任务进度 */
    public TaskQueryResp queryTask(String jobId) {
        return ragRestClient.get()
                .uri(INGEST_PATH + "/{jobId}", jobId)
                .retrieve()
                .body(TaskQueryResp.class);
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
        return Flux.defer(() -> { // defer：每次订阅拿到各自独立的解码器状态，
            // 否则两路并发问答会互相污染残字节
            CharsetDecoder decoder = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPLACE)
                    .onUnmappableCharacter(CodingErrorAction.REPLACE);
            ByteArrayOutputStream carry = new ByteArrayOutputStream(8); // 只存被切开的半个字符

            Flux<String> texts = chunks.map(buf -> {
                try {
                    byte[] bytes = new byte[buf.readableByteCount()];
                    buf.read(bytes);
                    if (carry.size() > 0) {
                        carry.write(bytes);
                        bytes = carry.toByteArray();
                        carry.reset();
                    }
                    ByteBuffer in = ByteBuffer.wrap(bytes);
                    // ★ 必须用三参重载 + endOfInput=false，不能用单参 decoder.decode(in)。
                    //   单参那个是「一次性解完全部输入」的便利方法：它内部按 endOfInput=true 调三参，
                    //   最后还会 flush()，于是结尾那半截字符被当成「输入到此结束的坏字节」，
                    //   按 REPLACE 换成 U+FFFD 并吃掉，position 直接走到末尾 ——
                    //   hasRemaining() 恒为 false，下面的 carry 就永远拿不到东西。
                    //   （实测：8 字节 [E4 BD A0 E5 A5 BD E4 B8]
                    //      单参     → 解出 你好 + U+FFFD，position=8，remaining=0
                    //      三参false → 解出 你好，          position=6，remaining=2 ← 要的是这个）
                    //   false = 「后面还有输入」，这样解不完的残字节才会留在 in 里。
                    CharBuffer out = CharBuffer.allocate(bytes.length + 1); // UTF-8 每字节最多产出一个字符
                    CoderResult cr = decoder.decode(in, out, false); // 这个重载不抛受检异常，改看返回值
                    if (cr.isError()) { // REPLACE 策略下不该发生，留作防御
                        throw new RagApiException(502, "上游返回的内容不是合法 UTF-8");
                    }
                    if (in.hasRemaining()) {
                        carry.write(bytes, in.position(), in.remaining());
                    }
                    out.flip();
                    return out.toString();
                } catch (IOException e) {
                    throw new RagApiException(502, "读取上游响应失败：" + e.getMessage());
                } finally {
                    // DataBuffer 可能是 Netty 的池化缓冲区，不还回去就是内存泄漏
                    DataBufferUtils.release(buf);
                }
            }).filter(s -> !s.isEmpty()); // 整块都是残字节时解不出东西，别往下游发空串

            // 收尾：流结束时还留着残字节，说明最后那个字符真的被截断了，解成 U+FFFD 收场
            Mono<String> tail = Mono.fromSupplier(() -> {
                if (carry.size() == 0) {
                    return "";
                }
                byte[] rest = carry.toByteArray();
                carry.reset();
                // 输出缓冲给 rest.length + 1 就够：REPLACE 策略下每个坏字节最多产出一个 U+FFFD
                CharBuffer out = CharBuffer.allocate(rest.length + 1);
                // 这两个重载不抛受检异常，改看返回的 CoderResult；REPLACE 策略下不会是 error
                if (decoder.decode(ByteBuffer.wrap(rest), out, true).isError()) { // true = 输入到此结束
                    return "";
                }
                if (decoder.flush(out).isError()) {
                    return "";
                }
                out.flip();
                return out.toString();
            }).filter(s -> !s.isEmpty());

            return texts.concatWith(tail);
        });
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