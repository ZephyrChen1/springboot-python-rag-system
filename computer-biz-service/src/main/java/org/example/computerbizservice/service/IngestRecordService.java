package org.example.computerbizservice.service;

import com.baomidou.mybatisplus.extension.conditions.update.LambdaUpdateChainWrapper;
import com.baomidou.mybatisplus.spring.service.impl.ServiceImpl;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.common.AppConstants;
import org.example.computerbizservice.common.enums.IngestStatus;
import org.example.computerbizservice.dto.response.rag.FileUploadResp;
import org.example.computerbizservice.dto.response.rag.TaskQueryResp;
import org.example.computerbizservice.entity.IngestTask;
import org.example.computerbizservice.exception.RagApiException;
import org.example.computerbizservice.mapper.IngestTaskMapper;
import org.example.computerbizservice.util.TextUtils;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.time.LocalDateTime;

/**
 * 入库任务的落库。三个方法对应三个写入时机，都由 {@code RagService} 调用。
 *
 * <p>继承 {@code ServiceImpl} 是为了拿到 {@code save / updateById / lambdaQuery / lambdaUpdate}
 * 这些便利方法，不用自己注入 mapper；泛型是 {@code <Mapper, 实体>}。
 *
 * <p>★ <b>包名和网上教程不一样</b>：MyBatis-Plus 3.5.17 重构过，老路径
 * {@code com.baomidou.mybatisplus.extension.service.impl.ServiceImpl} <b>在这个版本里是空的</b>
 * （那样写会报「程序包不存在」）。现在的位置是
 * {@code com.baomidou.mybatisplus.spring.service.impl.ServiceImpl}，
 * 继承链是 {@code ServiceImpl → CrudRepository → AbstractRepository}，
 * 方法体大部分在 {@code mybatis-plus-extension} 的 {@code ...extension.repository.IRepository} 上。
 * 抄教程时留意这一点。{@code baseMapper} 仍然是 {@code CrudRepository} 上的
 * {@code @Autowired} 字段，由 Spring 注入。
 *
 * <h2>一行一任务，三处写入</h2>
 * <pre>
 *   上传受理  → {@link #begin}             insert 一行，status=INGESTING
 *   每次轮询  → {@link #applyPoll}         就地更新（没行就兜底 insert）
 *   轮询失败  → {@link #markPollFailed}    只记 error，通常不动 status
 * </pre>
 *
 * <h3>① {@link #begin}</h3>
 * <ul>
 *   <li>{@code job_id} 空/空白 → {@code log.warn} 后直接返回。它是这张表<b>唯一的更新目标</b>，
 *       没有它的行以后永远找不回来；而前端那边本来也会把这次上传当失败。</li>
 *   <li>insert 的字段就是「上传那一刻已知的全部」：jobId、文件名、大小、MIME、userId、
 *       {@code status=INGESTING}、{@code pollCount=0}。stage/result/elapsedSec 要等轮询，
 *       error 是「没问到」的意思，lastPolledAt 要等第一次轮询，时间戳交给数据库默认值 ——
 *       这些字段<b>全部不赋值</b>（留 null）。</li>
 *   <li>{@code fileName} 取上游回显的名字（那是用户后来在界面上看到的名字），拿不到时才退回
 *       multipart 里的原始文件名；{@code originalFileName} 始终存用户真正传的那个名字 ——
 *       两个分开存，是为了事后能回答「用户传的到底是哪个名字」（上游可能改名/去空格）。</li>
 *   <li>上游已经 {@code done=true} 时也<b>不特殊处理</b>：{@code FileUploadResp} 里没有 result，
 *       拿不到前缀就判不出终态，而前端无论如何都会轮询至少一次 —— 第一次轮询会写出真相。</li>
 * </ul>
 *
 * <h3>② {@link #applyPoll}</h3>
 * <p>分三种情况，顺序不能反：
 * <ol>
 *   <li><b>没这行 → 兜底 INSERT</b>（{@code log.warn} 一笔）后继续走下面的更新。
 *       什么时候会走到这儿：上传那次落库失败过，或者应用中间重启过（内存里的东西没了，
 *       而前端还在拿着 job_id 轮询）。这就是「上传那条路少写一行也不致命」的实现方式。</li>
 *   <li><b>这行已经是终态 → 什么都不做。</b> ★ 终态只写一次，见下面第 2 条坑。</li>
 *   <li>否则更新：stage、elapsedSec、lastPolledAt、error（清空）、pollCount 自增，
 *       {@code done=true} 时再写 status + result。</li>
 * </ol>
 *
 * <h3>③ {@link #markPollFailed}</h3>
 * <ul>
 *   <li><b>没这行 → 什么都不做</b>（不能凭空造一行：一个我们从没受理过的 job_id，
 *       不该因为一次失败就在库里冒出来）。</li>
 *   <li><b>已经是终态 → 什么都不做。</b></li>
 *   <li>{@code 404} → {@code status=UNKNOWN} + error 写上上游的原话。
 *       这一条是<b>例外里的例外</b>：404 虽然也是「轮询失败」，但它是上游<b>明确回答</b>了
 *       「这个任务不存在（只保留最近一个任务）」—— 那是一次成功的询问，所以 status 该跟着上游走。</li>
 *   <li>其它（502 / 超时 / 连不上）→ <b>status 不动</b>，只把「没问到什么」记进 error。
 *       前端就是靠状态码区分「404 = 失效，重试没意义」和「502 = 暂时故障，该重试」的。</li>
 * </ul>
 *
 * <h2>result 前缀 → 状态（②里用）</h2>
 * <pre>
 *   [成功] 开头  → SUCCESS
 *   [跳过] 开头  → SKIPPED
 *   [失败] 开头  → FAILED
 *   其它 / 空     → UNKNOWN，并把「认不出来的那个 result」记进 error，让这行能自解释
 * </pre>
 * ★ <b>认不出来的时候绝不能判 FAILED。</b>判错了用户会重传，而同一份文档入库两次是这个上游
 * 没有幂等键导致的真实事故（见 frontend/README.md 里那条规则）—— Java 侧只是把同一条原则
 * 搬到了数据库里。
 *
 * <h2>error 列上会放两类东西（都不影响 result 的含义）</h2>
 * ① 我们没问到的（网络/502/超时）；② 上游说了、但不是「入库结论」的话（404 的
 * 「任务不存在」、result 前缀认不出来）。分界是：<b>result 只装上游对「入库这件事」的结论</b>，
 * 其余情况一律进 error。
 *
 * <h2>四个必须记住的点</h2>
 * <ul>
 *   <li><b>清空 error 只能写空串，不能用 null</b>：实体补丁（{@code updateById}）里 null 字段
 *       不会进 SET 子句，写 null 等于「这列不动」—— 上一次失败留下的报错会永远挂在那一行上。
 *       <br>★ 反过来，本类更新走的是 {@code lambdaUpdate().set(...)}，那是<b>显式地写列</b>
 *       （哪怕值是 null 也会写进去）。两套语义正好相反，改这段代码时先想清楚用的是哪一套。</li>
 *   <li><b>终态只写一次</b>：不只是靠「先查一次再决定写不写」（那是 check-then-act，两个页签
 *       同时轮询同一个 job 时会互相覆盖），而是把 {@code status='INGESTING'} 直接写进 UPDATE 的
 *       WHERE 里 —— 这才是真正的保证，查那一次只是为了打日志说得清。</li>
 *   <li><b>pollCount 必须用 SQL 表达式自增</b>（{@code poll_count = poll_count + 1}），
 *       不能在 Java 里读出来 +1 再写回 —— 两个页签同时轮询会丢计数。</li>
 *   <li><b>不是键的字符串进库前过 truncate</b>：MySQL 8.4 严格模式下超长是报错
 *       （{@code Data too long for column}），不是静默截断，而这条路上的异常会发生在
 *       「必须保持健康」的轮询请求里。<b>但 job_id 故意不截断</b>：截断会写下一个和上游对不上的
 *       键，而它是唯一的对账线索 —— 超长就让那次写入报错（日志里看得见），比写进一个假键好。</li>
 * </ul>
 *
 * <h2>自己怎么验（需要 FastAPI 起着）</h2>
 * <pre>
 * SELECT id, job_id, file_name, original_file_name, status, LEFT(stage,20) stage, result,
 *        elapsed_sec, error, poll_count, last_polled_at, updated_at
 * FROM ingest_task ORDER BY id DESC LIMIT 5;
 * </pre>
 * 依次验：轮询几次看 stage 在变、poll_count 在涨；入库完成后 status 变终态且 result 有值，
 * 并且日志里出现那行「入库任务终态」；上传第二个文件后轮询第一个 job（会 404）→ status 变
 * UNKNOWN 且 error 里有「仅保留最近一个任务」；停掉 FastAPI 再轮询一个还在 INGESTING 的 job
 * → status <b>不变</b>、error 记了原因。
 */
@Service
@Slf4j
public class IngestRecordService extends ServiceImpl<IngestTaskMapper, IngestTask> {

    /** 上游 result 的三种前缀（契约，见 TaskQueryResp 的字段注释） */
    private static final String PREFIX_SUCCESS = "[成功]";
    private static final String PREFIX_SKIPPED = "[跳过]";
    private static final String PREFIX_FAILED = "[失败]";

    /** 非 404 的轮询失败，error 里会带上这句 —— 让看库的人知道前端还会再试 */
    private static final String RETRY_HINT = "轮询失败（前端会重试）: ";

    /**
     * ① 上传被上游受理之后。
     *
     * <p>前置条件：{@code file} 非 null（{@code RagService.upload} 在调用前就校验过），
     * 而且它只是<b>元信息来源</b> —— 文件字节已经流给上游了，这里只读文件名/大小/MIME。
     *
     * <p>调用方用 {@code safeWrite} 包着，所以这里抛异常只会变成一条日志。
     */
    public void begin(FileUploadResp resp, MultipartFile file) {
        if (resp == null || TextUtils.isBlank(resp.getJobId())) {
            // 没有 job_id 的行以后永远更新不到（job_id 是唯一的更新目标），不写
            log.warn("上传受理落库：上游没给 job_id，这次上传不落库（前端那边本来也会当失败处理）");
            return;
        }

        String originalName = file.getOriginalFilename();

        IngestTask task = new IngestTask();
        task.setJobId(resp.getJobId()); // ★ 不截断：见类注释最后一条
        task.setUserId(AppConstants.DEFAULT_USER_ID);
        // 上游回显的名字优先（用户后来在界面上看到的就是它），没有时退回用户真正传的名字
        String displayName = TextUtils.isBlank(resp.getFileName()) ? originalName : resp.getFileName();
        task.setFileName(TextUtils.truncate(displayName, AppConstants.MAX_FILE_NAME_CHARS));
        task.setOriginalFileName(TextUtils.truncate(originalName, AppConstants.MAX_FILE_NAME_CHARS));
        task.setFileSize(file.getSize());
        task.setContentType(TextUtils.truncate(file.getContentType(), AppConstants.MAX_CONTENT_TYPE_CHARS));
        task.setStatus(IngestStatus.INGESTING);
        task.setPollCount(0);
        // stage / result / elapsedSec / error / lastPolledAt / 两个时间戳都不赋值：
        //   前四个是「还不知道」（error 留 null 就是「没问到」这个语义），时间戳交给数据库默认值

        save(task); // 自增主键会被回填进 task.getId()
        log.debug("上传受理落库：job_id={} file={} {} 字节 → ingest_task id={}",
                task.getJobId(), task.getFileName(), task.getFileSize(), task.getId());
    }

    /**
     * ② 每次轮询成功之后。
     *
     * <p>调用方用 {@code safeWrite} 包着，所以这里抛异常只会变成一条日志 ——
     * 成功问到的那次轮询不该因为写库失败而变成前端看到的错误。
     */
    public void applyPoll(TaskQueryResp resp) {
        if (resp == null || TextUtils.isBlank(resp.getJobId())) {
            // 上游返回了一个看不懂的体（少了 job_id）→ 没有更新目标，别瞎猜是哪一行
            log.warn("轮询结果落库：响应里没有 job_id，跳过这次落库");
            return;
        }
        String jobId = resp.getJobId();
        IngestTask row = findByJobId(jobId);

        if (row == null) {
            // 兜底：上传那次落库失败过，或者应用中间重启过（内存里的任务没了，前端还拿着 job_id 轮询）
            log.warn("轮询结果落库：库里没有 job_id={} 的行，兜底补一行（上传时落库失败，或应用重启过）", jobId);
            IngestTask fresh = new IngestTask();
            fresh.setJobId(jobId);
            fresh.setUserId(AppConstants.DEFAULT_USER_ID);
            fresh.setFileName(TextUtils.truncate(resp.getFileName(), AppConstants.MAX_FILE_NAME_CHARS));
            fresh.setStatus(IngestStatus.INGESTING);
            // pollCount 不赋值：走数据库默认值 0，紧接着下面那次更新会把它加到 1（正好是这一轮的次数）
            save(fresh);
        } else if (isTerminal(row.getStatus())) {
            log.debug("轮询结果落库：job_id={} 已经是终态 {}，不再改写", jobId, row.getStatus());
            return;
        }

        boolean done = Boolean.TRUE.equals(resp.getDone());
        String result = resp.getResult();
        IngestStatus nextStatus = done ? mapResult(result) : null; // null = 不写 status 这一列
        String error = ""; // 默认「清空上次的报错」，见类注释第 1 条

        if (done && nextStatus == IngestStatus.UNKNOWN) {
            // 认不出前缀时把「认不出来的东西」记下来，否则这行 UNKNOWN 以后没人看得懂
            error = TextUtils.truncate(
                    TextUtils.isBlank(result)
                            ? "上游说结束了但没给 result，无法判断成功与否"
                            : "上游返回了无法识别的 result 前缀：" + result,
                    AppConstants.MAX_ERROR_CHARS);
        }

        // 只带要变的列：没 set 的列一律不进 SET 子句（比如 done != true 时的 status）
        LambdaUpdateChainWrapper<IngestTask> update = inFlightRow(jobId)
                .set(IngestTask::getStage, TextUtils.truncate(resp.getStage(), AppConstants.MAX_STAGE_CHARS))
                .set(IngestTask::getElapsedSec, resp.getElapsedSec())
                .set(IngestTask::getError, error);
        if (done) {
            update.set(IngestTask::getStatus, nextStatus)
                    .set(IngestTask::getResult, TextUtils.truncate(result, AppConstants.MAX_RESULT_CHARS));
        }

        if (!update.update()) {
            // WHERE 里带着 status='INGESTING'，所以 0 行 = 期间被另一路轮询写成终态了（或者行被删了）
            log.debug("轮询结果落库：job_id={} 未命中在跑的行（已被更早的轮询写成终态），这次不覆盖", jobId);
            return;
        }
        if (done) {
            log.info("入库任务终态：job_id={} file={} → {} result={}",
                    jobId, row.getFileName(), nextStatus, TextUtils.truncate(result, 120));
        }
    }

    /**
     * ③ 轮询失败时（{@code RagService} 会把异常原样再抛给前端，这里只负责记录）。
     *
     * <p>前置条件：{@code jobId} 由 {@code RagService.queryTask} 校验过非空；
     * {@code e} 来自 catch 块，非 null。
     */
    public void markPollFailed(String jobId, RagApiException e) {
        IngestTask row = findByJobId(jobId);
        if (row == null) {
            // 不能凭空造一行：我们从没受理过的 job_id，不该因为一次失败就出现在库里
            log.debug("轮询失败落库：库里没有 job_id={} 的行，不补写（我们没受理过它）", jobId);
            return;
        }
        if (isTerminal(row.getStatus())) {
            log.debug("轮询失败落库：job_id={} 已经是终态 {}，不再改写", jobId, row.getStatus());
            return;
        }

        String detail = TextUtils.truncate(e.getDetail(), AppConstants.MAX_ERROR_CHARS);
        LambdaUpdateChainWrapper<IngestTask> update = inFlightRow(jobId);

        if (e.getStatus() == 404) {
            // 上游明确回答「这个任务不存在」= 一次成功的询问，所以 status 跟着上游走；
            // 未捕获的 404 只意味着「问不到了」，那时才该保持 status 不动
            update.set(IngestTask::getStatus, IngestStatus.UNKNOWN)
                    .set(IngestTask::getError, detail);
        } else {
            // ★ status 故意不 set：不 set 就是「不改这一列」。库里的 status 只代表
            //   「上游说过什么」，这次没问到的东西记进 error —— 否则以后做读接口时，
            //   分不清「上游说未知」和「我们没问到」。
            update.set(IngestTask::getError,
                    TextUtils.truncate(RETRY_HINT + detail, AppConstants.MAX_ERROR_CHARS));
        }

        if (!update.update()) {
            log.debug("轮询失败落库：job_id={} 未命中在跑的行，这次不记", jobId);
        }
    }

    /**
     * 在跑的那一行的公共更新骨架，两个更新方法都用它。
     *
     * <p>两处 {@code WHERE} 是这张表的全部并发防线：
     * <ul>
     *   <li>{@code job_id = ?} —— 定位。唯一键保证最多一行（所以查询那边敢用 {@code one()}）。</li>
     *   <li>{@code status = 'INGESTING'} —— ★ <b>「终态只写一次」的真正保证</b>。
     *       不写它的话，「先查一次、发现是终态就不写」是 check-then-act：两个页签同时轮询
     *       同一个 job 时，两边都可能查到「还在跑」，然后后到的那次把 SUCCESS 改写掉。
     *       写进 WHERE 里就是数据库层面的原子判断 —— 0 行受影响 = 这次不该写。
     *       所以调用方拿到 false 时不是错误，是「有人先写了」。</li>
     * </ul>
     * <p>顺带更新的两个列（每次轮询都算，成功失败都算）：{@code lastPolledAt} 和自增的
     * {@code pollCount}。有了它们才分得清「任务卡住了」和「页面早就关了」——
     * 这两件事在库里长得一模一样。
     */
    private LambdaUpdateChainWrapper<IngestTask> inFlightRow(String jobId) {
        return lambdaUpdate()
                .eq(IngestTask::getJobId, jobId)
                .eq(IngestTask::getStatus, IngestStatus.INGESTING)
                .set(IngestTask::getLastPolledAt, LocalDateTime.now())
                .setSql("poll_count = poll_count + 1"); // 见类注释第 3 条：必须让 SQL 自己加
    }

    /** 按 job_id 查这一行。用 {@code one()}：job_id 上有唯一键，多于一行说明索引被改了，那时候该炸 */
    private IngestTask findByJobId(String jobId) {
        return lambdaQuery().eq(IngestTask::getJobId, jobId).one();
    }

    /** 终态 = 除了 INGESTING 以外的任何值（包括 null：那说明数据被外部动过，按「不该再写」处理） */
    private static boolean isTerminal(IngestStatus status) {
        return status != IngestStatus.INGESTING;
    }

    /** 上游的 result 前缀 → 状态。★ 认不出来一律 UNKNOWN，绝不判 FAILED（见类注释） */
    private static IngestStatus mapResult(String result) {
        if (result == null) {
            return IngestStatus.UNKNOWN;
        }
        if (result.startsWith(PREFIX_SUCCESS)) {
            return IngestStatus.SUCCESS;
        }
        if (result.startsWith(PREFIX_SKIPPED)) {
            return IngestStatus.SKIPPED;
        }
        if (result.startsWith(PREFIX_FAILED)) {
            return IngestStatus.FAILED;
        }
        return IngestStatus.UNKNOWN;
    }
}
