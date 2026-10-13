# 对接 FastAPI rag 服务 —— 最小可运行版本实现说明

> 目标：在已有 DTO 的基础上，补齐 controller / service / client / config 四层，
> 让 `computer-biz-service` 能真正调通 FastAPI 的两个接口。
>
> **状态：已实现**（本文原本是动手前的设计稿，第五节起仍按「设计稿 + 为什么」的口吻写，
> 但代码都已在仓库里；第九节是联调实录）。后来的**落库层**（MyBatis-Plus + MySQL）是另一份文档：
> `docs/落库设计说明.md` —— 那一层把本文的三个接口都挂上了写库的钩子，
> 所以 `RagService` 现在的样子比第五节 5.5 里写的多了些东西。

---

## 一、本文覆盖的范围

对接的是 FastAPI 侧这两个接口（从你现有 DTO 反推出来的契约）：

| 上游 FastAPI | 作用 | 对应 DTO |
| --- | --- | --- |
| `POST /v1/kb/ingest` | 上传文档触发入库，立即返回 job_id | `FileUploadResp` |
| `GET /v1/kb/ingest/{job_id}` | 用 job_id 查入库进度 | `TaskQueryResp` |
| 任意非 2xx | 错误体 `{"detail": "..."}` | `ApiErrorResp` |

本服务对外暴露的接口（我拟的路径，可以改）：

| 本服务 | 作用 |
| --- | --- |
| `POST /api/rag/documents` (multipart/form-data) | 前端上传文档 |
| `GET /api/rag/tasks/{jobId}` | 前端轮询进度 |
| `POST /api/rag/chat` (SSE) | 前端提问，回答以流返回（第九节，后来加的） |

---

## 二、为什么要多加一层 client

你的想法是 controller → service → clientconfig 三层，我建议在 service 和 config 之间
插一层 **client（调用层）**：

```
Controller  ──►  Service  ──►  Client  ──►  RestClient(RestClientConfig)  ──►  FastAPI
   HTTP入口      业务编排      拼请求/反序列化     连接/超时/错误翻译
```

理由——

1. **错误映射只写一次。** FastAPI 的 401/404/409/422 全是 `{"detail": ...}`。如果在
   service 里直接调 `RestClient`，每个方法都要自己判断状态码、自己解析 detail。
   放在 client + config 里，靠 `defaultStatusHandler` 统一翻译成异常，service 只看到
   「返回值」或「异常」两种结果。
2. **换协议不脏业务代码。** 以后上游改成 gRPC / 加签名头 / 加重试，只动 client；
   校验参数、落库、轮询这些业务逻辑不受影响。
3. **DTO 语义能分清。** `dto/response/**` 应该是「我返回给前端的体」。上游返回体现在
   直接放在那里，短期没问题（因为目前是原样透传），但一旦你给前端多加一个字段，
   就会发现没法复用同一个类了。

> 所以最终分层是 5 个包：`controller` / `service` / `client` / `config` / `exception`。
> 你原本的三层顺序是对的，只是少了 client 这一层。

---

## 三、先定一个选型：RestClient 还是 WebClient

这是写 config 之前**必须先定**的事，因为它决定 config 里写什么。

你的 `pom.xml:33-40` 同时引了两个：

```xml
<artifactId>spring-boot-starter-webflux</artifactId>
<artifactId>spring-boot-starter-webmvc</artifactId>
```

两个都在 classpath 时，Spring Boot 的 `WebApplicationType.deduceFromClasspath()` 会
**选 SERVLET 栈**（因为发现了 `DispatcherServlet`），也就是说：

- 你的 web 容器是 **MVC / 阻塞式** 的，webflux starter 实际只贡献了一个 `WebClient` 类；
- 如果继续用 WebClient，controller 是阻塞的，调用处就得 `.block()`，
  或者一路返回 `Mono<>`，会和你现有 MVC 风格打架。

**本文选 RestClient**：Spring 6.1+ 提供的阻塞式 HTTP 客户端，和 MVC 栈天然一致，
代码同步、好打断点，对这个「上传 + 轮询」的场景足够了。

> 如果后面确实要 SSE / 流式返回，再换 WebClient，届时只改 `RagClientConfig` +
> `RagClient` 两个文件，service 以上不用动——这也正是加 client 层的好处。
>
> 附带：定了 RestClient 之后，`spring-boot-starter-webflux` 这个依赖就没用了，可以删。
> 本次先不动 pom，删依赖单独做，避免一次改太多。

---

## 四、改动清单

### 新增文件

| 文件 | 职责 |
| --- | --- |
| `config/RagClientProperties.java` | 读 `application.yaml` 里的 `rag.*` |
| `config/RagClientConfig.java` | 造 `RestClient` bean：baseUrl + 错误翻译 |
| `exception/RagApiException.java` | 上游非 2xx 时抛的业务异常 |
| `client/RagClient.java` | 两个方法对应两个上游接口 |
| `service/RagService.java` | 业务编排（目前基本直通） |
| `controller/RagController.java` | 对外 HTTP 入口 |
| `exception/GlobalExceptionHandler.java` | 统一错误出口 |

### 修改文件

| 文件 | 改什么 |
| --- | --- |
| `dto/response/ApiErrorResp.java` | 加 `@AllArgsConstructor`，方便 `new ApiErrorResp(detail)` |
| `resources/application.yaml` | 加 `rag.base-url` + multipart 大小限制 |

`pom.xml` **本次不用改**（RestClient 来自已有的 `spring-web`）。

> 本轮之外还有两件后来发生的事，都不属于这一节：`/api/rag/chat`（第九节的联调产物）和
> 落库层（`docs/落库设计说明.md`）。目录结构见第六节末尾。

---

## 五、逐个文件：代码 + 为什么

### 5.1 `config/RagClientProperties.java`（新增）

```java
package org.example.computerbizservice.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

/** FastAPI 侧 rag 服务的连接配置，对应 application.yaml 里的 rag.* */
@Data
@Component
@ConfigurationProperties(prefix = "rag")
public class RagClientProperties {

    /** 根地址，不要以 / 结尾 */
    private String baseUrl = "http://localhost:8102";
}
```

**为什么这么写**

- `@ConfigurationProperties` 而不是在方法上写 `@Value("${rag.base-url}")`：
  baseUrl 只是第一个，后面必然要加超时、鉴权 token 等，写在类里加字段就行，
  不用满项目找 `@Value`。
- 给了默认值，所以本地起服务时**不配 yaml 也能跑**，方便你先验证连通性。
- 放在 `config` 包下和 `RagClientConfig` 挨着，一眼能看出这是一组。

### 5.2 `config/RagClientConfig.java`（新增）

```java
package org.example.computerbizservice.config;

import com.fasterxml.jackson.databind.ObjectMapper; //核心json工具类，让java对象和json转换
import org.example.computerbizservice.dto.response.ApiErrorResp;
import org.example.computerbizservice.exception.RagApiException;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatusCode;
import org.springframework.web.client.RestClient;

import java.nio.charset.StandardCharsets; //标准字符集常量

@Configuration
public class RagClientConfig {

    /**
     * 注入的是 Spring Boot 自动装配的 RestClient.Builder（prototype），
     * 所以 spring.http.client.* 下的超时等配置能自动生效。
     */
    @Bean
    public RestClient ragRestClient(RestClient.Builder builder,
                                    RagClientProperties properties,
                                    ObjectMapper objectMapper) {
        return builder
                .baseUrl(properties.getBaseUrl())
                // FastAPI 的错误体统一是 {"detail": "..."}，在这里一次性翻译成异常，
                // 上层就不用每处都判断状态码了
                .defaultStatusHandler(HttpStatusCode::isError, (request, response) -> {
                    String body = new String(response.getBody().readAllBytes(), StandardCharsets.UTF_8);
                    String detail = parseDetail(objectMapper, body);
                    throw new RagApiException(response.getStatusCode().value(), detail);
                })
                .build();
    }

    private String parseDetail(ObjectMapper objectMapper, String body) {
        try {
            String detail = objectMapper.readValue(body, ApiErrorResp.class).getDetail();
            if (detail != null && !detail.isBlank()) {
                return detail;
            }
        } catch (Exception ignored) {
            // 上游没按约定返回 JSON，退化成原始文本
        }
        return body.isBlank() ? "上游返回空响应体" : body;
    }
}
```

**为什么这么写**

- **注入 `RestClient.Builder` 而不是 `RestClient.create()`**：Boot 自动装配了这个
  Builder，它已经挂好了 Jackson 消息转换器（multipart 要用到）和
  `spring.http.client.*` 的超时设置。自己 `RestClient.create()` 会丢掉这些。
- **`defaultStatusHandler` 在这里而不是在 client 里**：这是「所有调用共用的规则」，
  写一次全局生效。注意它读的是 `InputStream`，所以要先 `readAllBytes`，
  再交给 Jackson 解析。
- **`parseDetail` 有兜底**：上游 500 时可能返回 HTML 错误页或空 body，
  这时 `readValue` 会抛异常；如果直接把异常抛出去，你看到的报错就是
  「JSON 解析失败」而不是真正的问题，排查会很痛苦。
- **`HttpStatusCode::isError`** 是方法引用，等价于 `status -> status.isError()`。

### 5.3 `exception/RagApiException.java`（新增）

```java
package org.example.computerbizservice.exception;

import lombok.Getter;

/** FastAPI 返回非 2xx 时抛出，status/detail 原样带上，由 GlobalExceptionHandler 转成响应 */
@Getter
public class RagApiException extends RuntimeException {

    private final int status;
    private final String detail;

    public RagApiException(int status, String detail) {
        super(detail);
        this.status = status;
        this.detail = detail;
    }
}
```

**为什么这么写**

- 继承 `RuntimeException`：Spring 的 `@Transactional` 默认只对 RuntimeException 回滚，
  而且调用链上不用到处 `throws`。
- 同时带 status 和 detail：status 用来还原 HTTP 状态码，detail 是给前端看的文案。
- 用 `@Getter` 而不是 `@Data`：异常类不想要 setter 和 `equals`，`@Data` 太重。

### 5.4 `client/RagClient.java`（新增）

```java
package org.example.computerbizservice.client;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.springframework.core.io.Resource;
import org.springframework.http.MediaType;
import org.springframework.http.client.MultipartBodyBuilder;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.multipart.MultipartFile;

import java.util.Optional;

/**
 * 对 FastAPI rag 服务的调用层：一个方法对应一个上游接口，只做「拼请求 + 反序列化」。
 * 业务规则（重试、轮询、落库）放 RagService，不要写在这里。
 */
@Component //变成Bean，供后续业务service类使用
@RequiredArgsConstructor //对所有final成员变量生成构造函数
public class RagClient {

    private static final String INGEST_PATH = "/v1/kb/ingest";

    private final RestClient ragRestClient;

    /** POST /v1/kb/ingest，提交文档入库，立即返回 job_id */
    public FileUploadResp upload(MultipartFile file, FileUploadReq extra) {
        MultipartBodyBuilder body = new MultipartBodyBuilder(); //构建文件上传表单
        body.part("file", toResource(file))
                .filename(Optional.ofNullable(file.getOriginalFilename()).orElse("unknown"))
                .contentType(parseMediaType(file.getContentType())); //往表单注入文件

        return ragRestClient.post() //发送POST请求
                .uri(INGEST_PATH)
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

    private Resource toResource(MultipartFile file) {
        return file.getResource();
    } //MultipartFile是 Spring 接收前端上传文件的对象；file.getResource()把它转成 Spring 的 Resource，用来放进 Multipart 表单上传

    private MediaType parseMediaType(String contentType) {
        return contentType == null
                ? MediaType.APPLICATION_OCTET_STREAM
                : MediaType.parseMediaType(contentType);
    }
}
```

**为什么这么写**

- **`MultipartBodyBuilder` 是关键。** 上传 multipart 不能直接把 `MultipartFile`
  塞进去，要转成 `Resource` 并显式给 `filename` 和 `contentType`：
  - `filename` 不显式设置的话，FastAPI 侧 `UploadFile.filename` 可能是空的，
    而入库逻辑通常要靠文件名判断类型（.pdf / .md / .txt），会直接失败；
  - `contentType` 显式设置，避免上游拿到 `None` 又要猜。
  - 这是 Spring 官方推荐的多部分请求构造方式，`body.build()` 出来的是
    `MultiValueMap<String, HttpEntity<?>>`。
  - **注意**：`MultipartFile` 是 `spring-web` 里的 web 层类型，只在 client 的入参上
    出现是可以接受的（它就是个文件载体）；但不要在 `dto` 里出现，你在
    `FileUploadReq` 注释里写的「dto 只放额外文本参数，不放文件」是对的。
- **`FileUploadReq extra` 现在是空类**，方法体里只有一行 TODO。这是故意的：
  你原注释里预告了要加 `kbId`，参数先占好位置，加字段时只改这一行。
  （如果你觉得空参数看着别扭，也可以先不传，等有字段再加——两种都行。）
- **`.retrieve().body(X.class)`** 是 RestClient 的快捷写法，非 2xx 会走 config 里
  注册的 `defaultStatusHandler`，所以这里不需要写任何错误处理。
- **路径常量 `INGEST_PATH`**：两个方法用的是同一个路径前缀（一个 POST 一个 GET），
  抽出来避免手写两次对不上。

### 5.5 `service/RagService.java`（新增）

```java
package org.example.computerbizservice.service;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.client.RagClient;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

/**
 * 业务编排层。目前基本是直通，后面要加的轮询、重试、任务落库都写在这一层。
 */
@Service //交给 Spring 注册成 Bean，Controller 可以注入这个 RagService
@RequiredArgsConstructor
public class RagService {

    private final RagClient ragClient;

    public FileUploadResp upload(MultipartFile file, FileUploadReq extra) {
        if (file == null || file.isEmpty()) {
            throw new IllegalArgumentException("file 不能为空");
        }
        return ragClient.upload(file, extra);
    }

    public TaskQueryResp queryTask(String jobId) {
        if (jobId == null || jobId.isBlank()) {
            throw new IllegalArgumentException("jobId 不能为空");
        }
        return ragClient.queryTask(jobId);
    }
}
```

**为什么这么写**

- 现在这一层看起来「什么都没做」，但这正是分层的意义：**它是以后唯一会长大的地方**。
  你已经有的 `job_id` 说明上游是异步入库，那么「前端轮询 → 我这边缓存 job 状态 →
  超时判定 → 写业务表」这些一定落在 service，不会落在 client，也不该落在 controller。
- 参数校验放在 service 而不是全靠 `@NotBlank`：`@Valid` 只对 `@RequestBody` 生效，
  对文件和方法参数不生效（这点你在 `TaskQueryReq` 的注释里已经意识到了）。
- 抛 `IllegalArgumentException` 而不是 500：由全局异常处理器翻译成 400，
  语义上「你传错了」和「我们崩了」要区分开。

### 5.6 `controller/RagController.java`（新增）

```java
package org.example.computerbizservice.controller;

import lombok.RequiredArgsConstructor;
import org.example.computerbizservice.dto.request.rag.FileUploadReq;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.service.RagService;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ModelAttribute; //接收表单里的其他普通文本参数,比如kb_id(未加)
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestPart; //用来接收表单里的文件字段，对应前端表单的 file
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

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
}
```

**为什么这么写**

- **`FileUploadReq` 用 `@ModelAttribute` 不是 `@RequestBody`**：multipart 请求里，
  文件以外的文本参数是 form field，不是 JSON body。用 `@RequestBody` 会直接 415/400。
  （这也是为什么 `FileUploadReq` 里**不该**加 `@JsonProperty`——它不经过 Jackson 的
  JSON 反序列化，而是 Spring 的 form 绑定。）
- **`@RequestPart("file")` 不是 `@RequestParam`**：`@RequestPart` 走消息转换器，
  带原始文件名和 Content-Type；`@RequestParam` 对文件的支持是次要路径。
  名字必须和 FastAPI 侧的 `file: UploadFile = File(...)` 参数名一致。
- **`consumes = MULTIPART_FORM_DATA_VALUE`**：能让 Swagger/OpenAPI 生成正确的
  文档，也能让前端用错 Content-Type 时快速失败，而不是收到一个莫名其妙的 400。
- **controller 里只有转发**，没有任何 `if`。这样前端改路径、加鉴权都在这一层，
  不会渗到 service。

### 5.7 `exception/GlobalExceptionHandler.java`（新增）

```java
package org.example.computerbizservice.exception;

import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.dto.response.ApiErrorResp;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity; //完整控制 HTTP 返回结果
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

//**统一处理所有 Controller 抛出的异常，返回和 FastAPI 风格一致的错误 JSON `{"detail":"xxx"}`**，不用在每个接口单独写错误返回。
@Slf4j //自动生成 log 日志对象，用来打印日志
@RestControllerAdvice //监听所有 @RestController 抛出的异常
public class GlobalExceptionHandler {

    @ExceptionHandler(RagApiException.class)
    public ResponseEntity<ApiErrorResp> handleRagApi(RagApiException e) { //抛出 RagApiException(status,detail) 时触发
        log.warn("调用 rag 服务失败: status={}, detail={}", e.getStatus(), e.getDetail()); //打印日志同时记录状态码和详情
        HttpStatus status = HttpStatus.resolve(e.getStatus());
        // 上游的状态码透传给前端更好定位问题，只有解析不出来的才退回 502
        return ResponseEntity.status(status != null ? status : HttpStatus.BAD_GATEWAY)
                .body(new ApiErrorResp(e.getDetail())); //构造响应体
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ApiErrorResp> handleBadRequest(IllegalArgumentException e) {
        return ResponseEntity.badRequest().body(new ApiErrorResp(e.getMessage()));
    }//固定状态码

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiErrorResp> handleUnexpected(Exception e) {
        log.error("未预期异常", e);
        return ResponseEntity.internalServerError().body(new ApiErrorResp("服务内部错误"));
    }
}
```

**为什么这么写**

- **`@RestControllerAdvice` 而不是每个 controller 里 try-catch**：错误处理集中在
  一处，`RagController` 才能保持「只有一行转发」的干净样子。
- **错误体复用 `ApiErrorResp`**：让前端只需要认一种错误结构（`{"detail": ...}`），
  不管是本服务的参数错误还是 FastAPI 的错误。这也是我把 `ApiErrorResp` 保留在
  `dto/response/` 的原因——它现在同时是「上游错误体」和「我自己的错误体」。
- **三个 handler 的粒度**：上游错误（透传状态码）/ 客户端参数错误（400）/
  兜底未知异常（500）。兜底那条**必须 `log.error` 打完整堆栈**，否则线上出问题
  你只会看到一个光秃秃的 "服务内部错误"。
- **透传上游状态码的取舍**：也可以统一转成 502（语义上「上游故障」更准确），
  但那样前端拿不到 409/422 这类有用的信息。当前选择透传，等业务复杂了
  可以再改成「只透传 4xx，5xx 转 502」。

### 5.8 修改 `dto/response/ApiErrorResp.java`

只加一个注解：

```java
package org.example.computerbizservice.dto.response;

import lombok.AllArgsConstructor;
import lombok.Data;

/** FastAPI 统一错误体：{"detail": "..."}，401/404/409/422 都是这个结构；本服务的错误响应也复用它 */
@Data
@AllArgsConstructor
public class ApiErrorResp {
    private String detail;
}
```

**为什么**：`@Data` 只生成 required-args 构造器（这里等于无参），
`new ApiErrorResp(e.getDetail())` 编译不过。加 `@AllArgsConstructor` 最省事。

### 5.9 修改 `resources/application.yaml`

```yaml
spring:
  application:
    name: computer-biz-service
  servlet:
    multipart: //文件上传配置
      # 上传的是知识库文档，默认 1MB 肯定不够
      max-file-size: 100MB
      max-request-size: 100MB

rag:
  base-url: http://localhost:8102

logging:
  level:
    org.example.computerbizservice.client: DEBUG //打印DEBUG级别日志
```

**为什么这么写**

- **multipart 大小限制必须调**：Spring Boot 默认 `max-file-size=1MB` /
  `max-request-size=10MB`。知识库文档超了会直接抛
  `MaxUploadSizeExceededException`，而且是 500，看起来像服务挂了。
  这两个值要跟 FastAPI 侧的上限对齐。
- `rag.base-url` 指向本地 FastAPI（uvicorn 默认 8000）。
  部署时用环境变量覆盖：`RAG_BASE_URL=http://rag:8000`（`@ConfigurationProperties`
  天然支持这种 relaxed binding）。
- 开 client 包的 DEBUG 日志，能看到实际发出的请求，联调时很有用。

---

## 六、改动之后的目录结构

```
src/main/java/org/example/computerbizservice/
├── ComputerBizServiceApplication.java    ← 加 @MapperScan
├── client/
│   └── RagClient.java                    ← 新增
├── common/
│   ├── AppConstants.java                 ← 新增（落库）
│   ├── PersistenceStartupCheck.java      ← 新增（落库）
│   └── enums/
│       ├── ChatMsgStatus.java            ← 新增（落库）
│       ├── ChatRole.java                 ← 新增（落库）
│       └── IngestStatus.java             ← 新增（落库）
├── config/
│   ├── RagClientConfig.java              ← 新增
│   └── RagClientProperties.java          ← 新增
├── controller/
│   └── RagController.java                ← 新增
├── dto/
│   ├── request/rag/
│   │   ├── FileUploadReq.java            （不动）
│   │   └── TaskQueryReq.java             （不动，见下方遗留问题）
│   └── response/
│       ├── ApiErrorResp.java             ← 加 @AllArgsConstructor
│       └── rag/
│           ├── FileUploadResp.java       （不动）
│           └── TaskQueryResp.java        （不动）
├── entity/                               ← 以下四包整包都是落库那轮新增的，
│   ├── ChatMessage.java                    见 docs/落库设计说明.md
│   ├── ChatSession.java
│   ├── IngestTask.java
│   └── User.java
├── exception/
│   ├── GlobalExceptionHandler.java       ← 新增
│   └── RagApiException.java              ← 新增
├── mapper/
│   ├── ChatMessageMapper.java
│   ├── ChatSessionMapper.java
│   ├── IngestTaskMapper.java
│   └── UserMapper.java
├── service/
│   ├── ChatRecordService.java
│   ├── IngestRecordService.java
│   └── RagService.java                   ← 新增；落库那轮在里面挂了三个写入钩子
└── util/
    └── TextUtils.java

db/schema.sql                                ← 落库那轮新增（手工在 Navicat 里跑）
src/test/resources/application.yaml          ← 落库那轮新增（测试连 computer_biz_test）
docs/落库设计说明.md                          ← 落库那轮新增
```

---

## 七、跑起来怎么验证

> 注意端口：本服务默认 **8080**，FastAPI 是 **8000**，下面访问的是本服务。

1. 先起 FastAPI（假设在 8000 端口）。
2. 起本服务：`./mvnw spring-boot:run`
3. 上传一个文档：

```bash
curl -v -F "file=@./test.pdf" http://localhost:8080/api/rag/documents
```

期望返回：

```json
{"job_id": "xxx", "file_name": "test.pdf", "done": false}
```

4. 拿 job_id 查进度：

```bash
curl http://localhost:8080/api/rag/tasks/xxx
```

5. 验证错误通路（很重要，别只测 happy path）：
   - 传一个不存在的 jobId → 应该看到 FastAPI 的 404 detail 被原样透传，
     且 JSON 是 `{"detail": "..."}`；
   - 不传 file 字段 → 应该是 400 且 body 是 `{"detail": "..."}`，
     而不是 Spring 默认的那一坨 `{"timestamp":..., "path":...}`。
     如果看到了默认格式，说明 `GlobalExceptionHandler` 没生效
     （检查包路径是否在 `org.example.computerbizservice` 之下）。

---

## 八、遗留问题 / 下一步

按优先级排：

1. **`spring.http.client` 超时没配。** 现在用的全是默认值，FastAPI 卡住时本服务的
   线程会一直挂着。建议在 `application.yaml` 补 connect/read timeout
   （具体属性名以你 Boot 4.1 的文档为准，先在 IDE 里补全看一下
   `spring.http.client.` 下面有什么）。**这个建议尽快加。**
2. **`TaskQueryReq` 目前是死代码。** 它的 `@NotBlank` 只对 `@RequestBody` 生效，
   而 `jobId` 是 URL 路径参数（用 `@PathVariable`）。两条路选一条：
   - 直接删掉，路径上用 `@PathVariable @NotBlank String jobId` + 类上 `@Validated`；
   - 或者如果 `jobId` 将来确实要走 JSON body，就保留。
3. **`pom.xml` 里的 webflux starter 可以删**（见第三节）。删之前确认没有别的代码
   用到 `WebClient`。
4. **`FileUploadResp` / `TaskQueryResp` 的归属**：目前是原样透传给前端，放在
   `dto/response/rag` 是合理的。等到你要给前端加字段（比如加个 `jobId` 的
   展示用别名、加个本地任务状态），就该把它们挪到 `client/dto`，另建一套对外的 VO。
   现在不用急。
5. ~~**轮询、重试、任务落库**~~：**任务落库已经做完**（2026-09-17），
   见 `docs/落库设计说明.md`。**重试仍未做** —— 目前上游 5xx 会直接抛给前端，
   如果是网络抖动这种可以靠重试解决；做的时候记得它是「轮询失败只记 error、不改 status」
   这条规则的下游（见落库文档第六节），重试次数也该顺手记进 `ingest_task`（列已经有了：
   `poll_count`）。

---

## 九、`/ai/chat` 联调实录：三个坑（2026-09-16 修复）

问答这条路第一次接通时，前端只看到一句「后端返回了 500 但没带错误详情」。
一路挖下去是**三个独立的问题叠在一起**，每一个单独出现都会让人查错方向。
本节按「现象 → 实测证据 → 修法」记下来。

> 这一节之后，第五节里 `RagClientConfig` / `RagApiException` / `RagClient` /
> `GlobalExceptionHandler` 的代码片段已经过时，以本节为准。

### 坑 1：错误详情被 Accept 协商吃掉了（现象最误导）

前端发 `Accept: text/event-stream`，而 `/api/rag/chat` 的 `produces` 也是
`text/event-stream`。于是 `GlobalExceptionHandler` 想返回 `ApiErrorResp` 时，
**没有任何转换器能把 POJO 写成 text/event-stream**，Spring 只能吐一个
「没有 Content-Type、body 为空」的裸响应：

```
Accept: application/json   -> 406  {"detail":"Acceptable representations: [text/event-stream]."}
Accept: text/event-stream  -> 500  ct=null  body=""      ← 浏览器走这条，什么都看不到
Accept: 通配符             -> 500  {"detail":"服务内部错误"}   ← detail 明明写出来了
```

**修法**：`GlobalExceptionHandler` 里所有错误出口收敛到一个私有方法，
在里面显式 `.contentType(MediaType.APPLICATION_JSON)`。`ResponseEntity` 上
预设了具体 Content-Type 就不会再走 Accept 协商。

### 坑 2：Jackson 把 `null` 也发了出去，上游 422

`RagClient.aiChat` 抛的是：

```
WebClientResponseException$UnprocessableContent: 422 Unprocessable Content from POST http://localhost:8102/ai/chat
```

上游 `/ai/chat` 的模型是 `user_id: int = 0` —— **非 Optional 的 int**：

| 请求体 | 上游响应 |
| --- | --- |
| `{"user_id":null, ...}` | **422** `Input should be a valid integer` |
| `{"user_id":0, ...}` | 200 |
| 不带 `user_id` | 200（用默认值 0） |

而 `AiChatReq.userId` 是包装类型 `Integer`，前端不发时是 `null`，
Jackson **默认会把 null 字段也序列化**，于是 Java 老老实实发出了
`"user_id": null` → 422。`top_k` 同理：原来是基本类型 `int`，
前端不传时序列化成 `"top_k": 0`，而上游要求 1~100，又是一个 422。

**修法**：`AiChatReq` 加 `@JsonInclude(NON_NULL)`（类级别），`topK` 改成 `Integer`。
语义上「字段缺失 = 用上游默认值」，空值就该整个不发。

### 坑 3：上游声明 SSE，发的却是裸文本 —— 整条回答静默消失

上游 `/ai/chat` 的 `Content-Type` 是 `text/event-stream`，但吐出来的字节是
**裸 UTF-8 文本，没有 `data:` 分帧**：

```bash
curl -sN -X POST :8102/ai/chat ... | head -c 120 | od -c
# 0000000 344 275 240 345 245 275 ...   ← 直接就是中文字节，没有 "data:" 前缀
```

Spring 看到 `text/event-stream` 会选 `ServerSentEventHttpMessageReader` 去解析
「SSE 事件」，而按 SSE 规范，不带 `data:`/`event:`/`id:`/`retry:` 前缀的行是
**未知字段，直接丢弃**。结果：解析出 0 个事件 → `bodyToFlux(String.class)` 是个空流
→ 接口返回 **200、流是空的**，前端白等（这个比报错更难查）。

编解码器的选择是**按响应头的 Content-Type 定的，请求头 `Accept` 改不了它**，
所以只能从客户端这一层绕开编解码器：

```java
.exchangeToFlux(response -> {
    if (response.statusCode().isError()) { /* 见下 */ }
    return decodeRawText(response.bodyToFlux(DataBuffer.class));  // 拿原始字节，别让 SSE 解析器碰
});
```

配套的 `decodeRawText` 还要自己扛住「块边界切开一个汉字」（UTF-8 里汉字 3 字节），
用一个跨块保留残字节的 `CharsetDecoder`，和前端 `TextDecoder({stream:true})` 是同一件事。
每块各自 `new String(bytes)` 会得到乱码。

### 顺手补齐的两处

1. **WebClient 没有错误映射**：`ragRestClient` 上有 `defaultStatusHandler`，
   而 `ragWebClient` 上没有，所以上游任何 4xx/5xx 到这里都变成
   `WebClientResponseException` → 被 catch-all 兜成「服务内部错误」，
   上游的真实原因全丢了。现在在 `exchangeToFlux` 里映射成 `RagApiException`。
   （流式请求不能用 `defaultStatusHandler`：它在响应头刚到时就抛异常，
   对流式响应会把连接掐掉。）
2. **`detail` 只有一份翻译实现**：抽成 `RagApiException.fromUpstream(status, body, mapper)`，
   两个客户端共用。顺带让它认 FastAPI 的**第二种错误体** ——
   参数校验失败的 `detail` 是**数组**：

   ```json
   {"detail":[{"type":"int_type","loc":["body","user_id"],
               "msg":"Input should be a valid integer","input":null}]}
   ```

   原来用 `ApiErrorResp(String detail)` 反序列化会直接抛异常，退化成
   「整段原始 JSON 当错误文案」。现在翻成 `user_id: Input should be a valid integer`。
   这次排查的 20 分钟，有一半是花在读这段没翻的 JSON 上。

### 验证方式

```bash
# 上游直连（对照用，确认上游本身没问题）
curl -sN -X POST http://127.0.0.1:8102/ai/chat -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' -d '{"session_id":"probe-1","message":"hi","top_k":5}'

# 经 Java（前端的确切请求头）——修复前后差别就在这条
curl -sN -X POST http://127.0.0.1:8080/api/rag/chat -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' --data-binary @body.json

# 上游 4xx 应该原样透出状态码 + 翻好的 detail，而不是 500「服务内部错误」
curl -sN -X POST http://127.0.0.1:8080/api/rag/chat -H 'Accept: text/event-stream' \
  -d '{"session_id":"probe-1","message":"hi","top_k":101}'
# -> 422 {"detail":"top_k: Input should be less than or equal to 100"}
```

前端侧端到端：`node frontend/tools/verify-chat-ui.cjs`（需三个进程都在跑）。

