package org.example.computerbizservice.common.enums;

import com.baomidou.mybatisplus.annotation.EnumValue;

/**
 * 一条消息是谁说的。
 *
 * <p>这里的 code 是<b>小写</b>，和前端 {@code chatStore.js} 里的 role 取值完全一样
 * （那两个状态枚举用的是大写，别被这个不一致绊到：role 的值在前端和接口上都是
 * {@code 'user'/'assistant'} 这个小写形式，数据库跟着写小写，跨层 grep 才搜得到）。
 */
public enum ChatRole {

    /** 用户提问 */
    USER("user"),

    /** 模型回答。正文由 Java 代理 SSE 时自己攒 —— 上游不会把答案回传给我们 */
    ASSISTANT("assistant");

    @EnumValue
    private final String code;

    ChatRole(String code) {
        this.code = code;
    }

    public String getCode() {
        return code;
    }
}
