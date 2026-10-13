package org.example.computerbizservice.common.enums;

import com.baomidou.mybatisplus.annotation.EnumValue;

/**
 * 入库任务状态。落库时存的是 {@code code}（大写字符串），既不是序号也不是 default 的写法。
 *
 * <p>★ 这五个值对应前端 {@code ingestStore.js} 里 {@code STATUS} 的<b>后五个</b>（小写）：
 * ingesting / success / skipped / failed / unknown。前端的 queued / uploading / committing
 * 是纯客户端状态（那时请求还在网线上），服务端永远看不到，所以这里没有它们。
 *
 * <p>★ 为什么显式写 {@code @EnumValue}：MyBatis-Plus 3.5 的默认枚举处理器行为变过，
 * 不写明的话「到底存枚举名还是存序号」得翻版本才知道。写出来之后，Navicat 里看到的是
 * 可读的 {@code 'INGESTING'}，而不是 {@code 3}。检查方法见 docs/落库设计说明.md。
 *
 * <p>怎么从上游响应映射到这些值（前缀判断那套）写在
 * {@code service/IngestRecordService} 里 —— 那是业务判断，不放这里。
 */
public enum IngestStatus {

    /** 已受理，上游还在异步入库。上传受理时的初值，也是 done != true 时每次轮询的取值 */
    INGESTING("INGESTING"),

    /** 上游说成了：result 以 [成功] 开头 */
    SUCCESS("SUCCESS"),

    /** result 以 [跳过] 开头（比如内容已存在） */
    SKIPPED("SKIPPED"),

    /** result 以 [失败] 开头。★ 只有上游能给出这个状态，Java 侧永远不主动判失败 */
    FAILED("FAILED"),

    /**
     * 不知道结果。★ <b>不等于失败</b>：上游只保留最近一个 job，旧 job 的轮询会 404，
     * 那属于「没问到」而不是「失败了」——前端是同一条原则，否则用户会重传，
     * 导致同一份文档入库两次（上游没有幂等键，schema 也补不出来）。
     */
    UNKNOWN("UNKNOWN");

    @EnumValue // 告诉 MyBatis-Plus：写库时存这个字段的值，而不是 name() 或 ordinal()
    private final String code;

    IngestStatus(String code) {
        this.code = code;
    }

    public String getCode() {
        return code;
    }
}
