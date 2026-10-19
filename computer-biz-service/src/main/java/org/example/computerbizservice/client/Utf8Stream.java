package org.example.computerbizservice.client;

import org.example.computerbizservice.exception.RagApiException;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferUtils;

import reactor.core.publisher.Flux;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.CharBuffer;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.CoderResult;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 把上游响应体按原始字节解成文本 / 按行切分。
 *
 * <p>两件事必须在这一层做，不能交给 Spring 的消息读取器：
 * <ul>
 *   <li><b>不能直接 {@code bodyToFlux(String.class)}</b>：上游 /ai/chat 声明了
 *       {@code text/event-stream} 却吐裸文本，Spring 会选 SSE 解析器把没有
 *       {@code data:} 前缀的行直接丢掉（整条回答静默消失）。/ai/agent/chat 虽然
 *       声明的是 {@code application/x-ndjson}，但同样没有必要让读取器替我们猜。</li>
 *   <li><b>块边界会切开一个汉字</b>（UTF-8 里汉字 3 字节）。每块各自
 *       {@code new String(bytes)} 会得到乱码，所以维护一个残字节缓冲（carry），
 *       并配合 {@link CharsetDecoder} 的三参重载 + {@code endOfInput=false} 把解不完的
 *       字节留下来。这和前端 {@code TextDecoder({stream:true})} 是同一件事。</li>
 * </ul>
 */
public final class Utf8Stream {

    private Utf8Stream() {
    }

    /**
     * 原始文本流：不保证切分位置有意义（可能切在汉字、JSON、行的中间）。
     *
     * <p>★ {@code Flux.defer} 不能省：每个订阅要拿到自己独立的解码器和 carry，
     * 否则两路并发请求会互相污染残字节。
     */
    public static Flux<String> decode(Flux<DataBuffer> chunks) {
        return Flux.defer(() -> {
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
                    // ★ 必须用三参重载 + endOfInput=false。单参 decode(in) 内部按
                    //   endOfInput=true 调三参并 flush()，结尾那半截字符会被当成坏字节
                    //   替换掉，position 直接走到末尾 —— carry 永远拿不到东西。
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
            Flux<String> tail = Flux.defer(() -> {
                if (carry.size() == 0) {
                    return Flux.empty();
                }
                byte[] rest = carry.toByteArray();
                carry.reset();
                CharBuffer out = CharBuffer.allocate(rest.length + 1);
                if (decoder.decode(ByteBuffer.wrap(rest), out, true).isError()) { // true = 输入到此结束
                    return Flux.empty();
                }
                if (decoder.flush(out).isError()) {
                    return Flux.empty();
                }
                out.flip();
                String text = out.toString();
                return text.isEmpty() ? Flux.empty() : Flux.just(text);
            });

            return texts.concatWith(tail);
        });
    }

    /**
     * 按行切分（NDJSON）：每个元素是一行完整、非空的文本，不含行尾。
     *
     * <p>上游的 {@code json.dumps} 会把内容里的换行转义成 {@code \n}，所以「一行一个
     * JSON 对象」这条约定是可靠的。这里仍然做了防御：不完整的尾巴留在缓冲区里等下一块，
     * 流结束时若还有非空尾巴就作为最后一行发出去（很多实现不发尾随换行）。
     */
    public static Flux<String> lines(Flux<DataBuffer> chunks) {
        return Flux.defer(() -> {
            StringBuilder buffer = new StringBuilder(256);

            Flux<String> complete = decode(chunks)
                    .concatMapIterable(text -> takeLines(buffer, text));

            Flux<String> tail = Flux.defer(() -> {
                String rest = buffer.toString().strip();
                buffer.setLength(0);
                return rest.isEmpty() ? Flux.empty() : Flux.just(rest);
            });

            return complete.concatWith(tail);
        });
    }

    /** 追加新文本，取出其中所有已完整的行，剩下的半行留在 buffer 里 */
    private static List<String> takeLines(StringBuilder buffer, String text) {
        buffer.append(text);
        List<String> lines = new ArrayList<>();
        int idx;
        while ((idx = buffer.indexOf("\n")) >= 0) {
            String line = buffer.substring(0, idx).strip(); // 顺带去掉 \r
            buffer.delete(0, idx + 1);
            if (!line.isEmpty()) {
                lines.add(line);
            }
        }
        return lines;
    }
}
