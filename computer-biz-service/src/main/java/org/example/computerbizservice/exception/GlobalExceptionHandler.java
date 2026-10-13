package org.example.computerbizservice.exception;

import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.dto.response.ApiErrorResp;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity; //完整控制 HTTP 返回结果
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.http.ProblemDetail;
import org.springframework.web.ErrorResponse;

//**统一处理所有 Controller 抛出的异常，返回和 FastAPI 风格一致的错误 JSON `{"detail":"xxx"}`**，不用在每个接口单独写错误返回。
@Slf4j //自动生成 log 日志对象，用来打印日志
@RestControllerAdvice //监听所有 @RestController 抛出的异常
public class GlobalExceptionHandler {

    @ExceptionHandler(RagApiException.class)
    public ResponseEntity<ApiErrorResp> handleRagApi(RagApiException e) { //抛出 RagApiException(status,detail) 时触发
        log.warn("调用 rag 服务失败: status={}, detail={}", e.getStatus(), e.getDetail()); //打印日志同时记录状态码和详情
        // 上游的状态码透传给前端更好定位问题，只有解析不出来的才退回 502
        HttpStatus status = HttpStatus.resolve(e.getStatus());
        return error(status != null ? status : HttpStatus.BAD_GATEWAY, e.getDetail()); //构造响应体
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ApiErrorResp> handleBadRequest(IllegalArgumentException e) {
        return error(HttpStatus.BAD_REQUEST, e.getMessage());
    }//固定状态码

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiErrorResp> handleUnexpected(Exception e) {
        // Spring 自带的 web 异常（415/400/405…）本身带着正确的状态码，不能和真正的未知异常一样按 500 处理。
        // 注意只能用 instanceof 判断：ErrorResponse 是接口、不是 Throwable，
        // 写成 @ExceptionHandler(ErrorResponse.class) 会因为 value() 要求 Class<? extends Throwable>[] 而编译不过。
        if (e instanceof ErrorResponse errorResponse) {
            HttpStatus status = HttpStatus.resolve(errorResponse.getStatusCode().value());
            ProblemDetail body = errorResponse.getBody();
            String detail = (body != null && body.getDetail() != null)
                    ? body.getDetail()
                    : (status != null ? status.getReasonPhrase() : "请求不合法");
            log.warn("请求不合法: status={}, detail={}", status, detail);
            return error(status != null ? status : HttpStatus.BAD_REQUEST, detail);
        }
        log.error("未预期异常", e);
        return error(HttpStatus.INTERNAL_SERVER_ERROR, "服务内部错误");
    }

    /**
     * 唯一的错误响应出口。
     *
     * ★ .contentType(APPLICATION_JSON) 这一句是**必需的**，不是装饰：
     * /api/rag/chat 的 produces 是 text/event-stream，而浏览器那条请求的 Accept 也是
     * text/event-stream，于是这里想写 ApiErrorResp 时**找不到能写这个对象的转换器**
     * （没有任何转换器能把 POJO 写成 text/event-stream），Spring 只能吐一个
     * 「没有 Content-Type、body 为空」的裸响应：
     *   Accept: text/event-stream -> 500  ct=null  body=""      ← 前端只能瞎猜
     *   Accept 用通配符            -> 500  {"detail":"..."}      ← detail 明明在这儿
     * 显式指定后就不再走 Accept 协商，错误体一定能写出去，前端也就能看到真实原因。
     */
    private static ResponseEntity<ApiErrorResp> error(HttpStatus status, String detail) {
        return ResponseEntity.status(status)
                .contentType(MediaType.APPLICATION_JSON)
                .body(new ApiErrorResp(detail));
    }
}