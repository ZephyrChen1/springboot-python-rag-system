package org.example.computerbizservice.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import org.example.computerbizservice.common.enums.IngestStatus;

import java.time.LocalDateTime;

/**
 * ingest_task 表：一行一个入库任务，随轮询就地更新（不是流水账）。
 *
 * <p>为什么是「就地更新」而不是「追加事件」：上游的轮询接口返回的是一份<b>快照</b>
 * （stage / result / elapsed / done），不是事件流。追加的话一次入库会攒下几百行几乎一样的记录，
 * 而「什么时候结束的」靠 created_at/updated_at 加上「终态只写一次」就够了。
 *
 * <p>★ 同样别加 {@code @Builder} / {@code @AllArgsConstructor}（理由见 {@link User}）。
 *
 * <p>★ 也<b>没有</b>加 {@code @TableLogic}（逻辑删除），理由：逻辑删除 + 唯一业务键 =
 * 唯一键失效（被「删掉」的行仍然占着 job_id），而这轮连读接口都没有，
 * 拿一个真实的索引语义去换零收益的删除能力不划算。用户要禁用走 {@code user.status}。
 */
@Data
@TableName("ingest_task")
public class IngestTask {

    @TableId(type = IdType.AUTO)
    private Long id;

    /** 上游 FastAPI 的 job_id，唯一。既是本表的更新目标，也是「同一份文档被传了两次」的唯一对账线索 */
    private String jobId;

    private Long userId;

    /** 上游回显的文件名 */
    private String fileName;

    /** 用户真正传的那个文件名（上游可能改名/去空格，所以和 fileName 分开存） */
    private String originalFileName;

    private Long fileSize;

    private String contentType;

    /** 见 {@link IngestStatus}：落库时存的是大写字符串（'INGESTING' 等） */
    private IngestStatus status;

    /** 上游的人类可读进度文案，每次轮询覆盖。只留最后一次 —— 它描述的是「当前状态」不是历史 */
    private String stage;

    /** 上游 done=true 时的结果文案，前缀是契约：[成功]/[跳过]/[失败] */
    private String result;

    private Double elapsedSec;

    /**
     * ★ 一行里除「入库结论」之外要说的话，两类：① 我们没问到的（网络 / 502 / 超时）
     * ② 上游说了、但不是入库结论的（404 的「任务不存在」、result 前缀认不出来——
     * 后一种是为了让 UNKNOWN 那行能自解释）。
     * 分界是：{@link #result} 只装<b>上游对「入库这件事」的结论</b>，其余情况一律进这里。
     *
     * <p>★ 清空它只能写空串 {@code ""}，<b>不能写 null</b> —— MP 默认不把 null 字段写进 UPDATE
     * 语句，写 null 等于什么都没发生，上一次的报错会永远留在那一行上。详见 RagService 里
     * 「落库是旁路」那段注释。
     */
    private String error;

    /** 被轮询的次数。「任务卡住了」和「页面早被关了」在库里长得一模一样，只有它能分开 */
    private Integer pollCount;

    private LocalDateTime lastPolledAt;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;
}
