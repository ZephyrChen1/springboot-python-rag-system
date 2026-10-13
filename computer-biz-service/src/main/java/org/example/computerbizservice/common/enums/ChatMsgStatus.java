package org.example.computerbizservice.common.enums;

import com.baomidou.mybatisplus.annotation.EnumValue;

/**
 * 一条问答消息的状态。落库存 {@code code}（大写字符串）。
 *
 * <p>五个值和前端 {@code chatStore.js} 里 {@code MSG} 的那五个<b>逐条对应</b>（大小写不同：
 * 前端小写、库里大写，各自的命名习惯）。<b>不要自己造第六个</b> —— 前端的状态机是这套语义的
 * 另一半，多加一个值它就渲染不出来。
 *
 * <p>谁把「流的结束方式」翻译成这里的值：{@code service/RagService} 里代理 SSE 的那条链
 * （它是唯一知道「有没有收到过内容」「是正常结束还是被取消」的地方）。
 */
public enum ChatMsgStatus {

    //前五个枚举值，对应前端的状态：聊天消息记录的状态
    /** 正在逐段生成。助手那行建出来时的初值，此时 content 还是空的 */
    STREAMING("STREAMING"),

    /** 正常结束（Flux 走完了 onComplete） */
    DONE("DONE"),

    /**
     * 用户点了「停止生成」。★ 不是故障：浏览器 abort 掉 fetch，连接断开，
     * 流被取消 —— 这时已经收到的半截回答是唯一的痕迹，所以要在终态里存下来。
     */
    CANCELED("CANCELED"),

    /** 收到了一部分但流没正常结束（上游报错 / 连接被掐）。和 ERROR 的区别是「有没有收到过东西」 */
    INTERRUPTED("INTERRUPTED"),

    /** 一个字都没收到就失败了 */
    ERROR("ERROR");

    @EnumValue
    private final String code; //告诉 MyBatis-Plus：入库、查询时，使用 code 这个字段的值和数据库映射

    ChatMsgStatus(String code) {
        this.code = code;
    } //枚举类的构造方法强制是 private

    public String getCode() {
        return code;
    }
}
