package org.example.computerbizservice.client;

import org.junit.jupiter.api.Test;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DefaultDataBufferFactory;

import reactor.core.publisher.Flux;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * NDJSON 分帧的离线测试：不启动 Spring、不连库。
 *
 * <p>要验证的只有两条契约：
 * <ol>
 *   <li>一个汉字被切在两个 DataBuffer 之间，解码后不能是乱码；</li>
 *   <li>一行 JSON 被切在两个 DataBuffer 之间，最终仍然恰好是一行（不能拆成两条事件）。</li>
 * </ol>
 */
class Utf8StreamTest {

    private static final DefaultDataBufferFactory FACTORY = new DefaultDataBufferFactory();

    @Test
    void splitsLinesAcrossBuffersAndKeepsChineseIntact() {
        String text = "{\"type\":\"answer\",\"delta\":\"你好，世界\"}\n"
                + "{\"type\":\"done\",\"steps\":2}\n";
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);

        // 每 3 字节一块：汉字正好是 3 字节，所以必然有被切断的字符和行
        List<DataBuffer> chunks = new ArrayList<>();
        for (int i = 0; i < bytes.length; i += 3) {
            int end = Math.min(i + 3, bytes.length);
            byte[] slice = new byte[end - i];
            System.arraycopy(bytes, i, slice, 0, slice.length);
            chunks.add(FACTORY.wrap(slice));
        }

        List<String> lines = Utf8Stream.lines(Flux.fromIterable(chunks))
                .collectList()
                .block();

        assertThat(lines).containsExactly(
                "{\"type\":\"answer\",\"delta\":\"你好，世界\"}",
                "{\"type\":\"done\",\"steps\":2}");
    }

    @Test
    void flushesLastLineWithoutTrailingNewline() {
        List<String> lines = Utf8Stream.lines(Flux.just(
                        FACTORY.wrap("{\"type\":\"answer\"}\n{\"type\":\"done\"}"
                                .getBytes(StandardCharsets.UTF_8))))
                .collectList()
                .block();

        assertThat(lines).containsExactly("{\"type\":\"answer\"}", "{\"type\":\"done\"}");
    }

    @Test
    void toleratesCrLfAndBlankLines() {
        List<String> lines = Utf8Stream.lines(Flux.just(
                        FACTORY.wrap("{\"a\":1}\r\n\r\n{\"b\":2}\r\n"
                                .getBytes(StandardCharsets.UTF_8))))
                .collectList()
                .block();

        assertThat(lines).containsExactly("{\"a\":1}", "{\"b\":2}");
    }
}
