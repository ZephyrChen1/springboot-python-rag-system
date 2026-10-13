package org.example.computerbizservice.common;

/**
 * 落库层用的常量。集中在一处，是因为它们几乎全都是「和 db/schema.sql 的列宽/取值对齐」的东西 ——
 * 散在各处的话，改列宽时会漏掉某一个。
 */
public final class AppConstants {

    private AppConstants() {
    }

    /**
     * 所有业务记录挂在这个用户上（{@code user} 表里由启动自检补写的演示用户）。
     *
     * <p>为什么是常量而不是配置项：本轮没有登录，这是「我们替用户声明的身份」；
     * 等登录做完，这里会换成「从 SecurityContext 里取」，那时配置项只是多一个要清理的开关。
     * ★ 绝不能改成「信任请求里带的 user_id」—— 没有鉴权的现在，那是白送的越权。
     */
    public static final long DEFAULT_USER_ID = 1L;

    /**
     * 一条回答最多攒多少个字符入库（≈300KB 的 utf8mb4）。
     *
     * <p>这个上限不是给正常回答用的，是防「上游挂在那里一直推」把内存和列宽推爆。
     * 超出的部分不再攒，<b>但流本身照常转发</b> —— 用户该看到的照看，落库是旁路。
     */
    public static final int MAX_STORED_ANSWER_CHARS = 100_000;

    /** 会话标题取提问的前多少个字符（列宽是 64，所以这里不用截断） */
    public static final int TITLE_SOURCE_CHARS = 30;

    // ---- 下面这些是 db/schema.sql 里的列宽，写入前一律过 TextUtils.truncate ----
    // 理由：MySQL 8.4 跑在严格模式下，超长**不截断而是报** Data too long for column，
    // 而轮询那条路上的异常会发生在「必须保持健康」的请求里。
    //
    // ★ 例外是**业务键**（job_id / session_id）：它们故意不截断，超长就让那次写入报错。
    //   截断一个键会写下一个和上游对不上的值，而那是唯一的对账线索 —— 假键比没键更坏。
    //   （所以这里的 MAX_* 里没有 job_id / session_id。列宽见 db/schema.sql。）

    public static final int MAX_FILE_NAME_CHARS = 255;
    public static final int MAX_STAGE_CHARS = 255;
    public static final int MAX_RESULT_CHARS = 512;
    public static final int MAX_ERROR_CHARS = 1024;
    public static final int MAX_CONTENT_TYPE_CHARS = 128;
}
