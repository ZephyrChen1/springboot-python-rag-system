package org.example.computerbizservice.util;

/** 写库前的小工具。 */  //放 无状态、纯静态 工具方法
public final class TextUtils {

    private TextUtils() {
    }

    /**
     * 按<b>字符</b>截断 —— 正好是 {@code VARCHAR(n)} 的口径（它的 n 数的是字符，不是字节）。
     *
     * <p>为什么要截：MySQL 8.4 跑在严格模式下，超长会直接报
     * {@code Data too long for column 'xxx'}，而不是悄悄截掉。而轮询那条路上的异常
     * 会发生在「必须保持健康」的请求里 —— 一个超长的上游文案不该把轮询搞挂。
     *
     * <p>null 原样返回：调用方有时是靠 null 和空串区分语义的（比如 error 列的「没写」和
     * 「清空」），这个函数不替它们做决定。
     */
    public static String truncate(String s, int maxChars) {
        if (s == null || s.length() <= maxChars) {
            return s;
        }
        return s.substring(0, maxChars); //字符串截取
    }

    /** null 和全空白都算空。 */
    public static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
