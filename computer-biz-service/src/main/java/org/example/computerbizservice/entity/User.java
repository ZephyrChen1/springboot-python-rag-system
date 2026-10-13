package org.example.computerbizservice.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName; //以上三个，做实体和数据库表映射
import lombok.Data;

import java.time.LocalDateTime;

/**
 * user 表。本轮只有一条演示数据（id = {@link org.example.computerbizservice.common.AppConstants#DEFAULT_USER_ID}，
 * 由 {@code common/PersistenceStartupCheck} 在启动时补写），没有登录、没有注册。
 *
 * <p>★ 两个 Lombok 的坑，加注解前先看这里：
 * <ul>
 *   <li><b>不要加 {@code @Builder} / {@code @AllArgsConstructor}</b>：MyBatis 靠<b>无参构造 +
 *       setter</b> 把结果集变成对象，加了全参构造就没有默认构造了，症状是「查出来的对象字段全是 null」，
 *       而且不报任何错。本项目 Lombok 用得多，这个坑很容易手滑。</li>
 *   <li>时间列（createdAt/updatedAt）<b>不加 {@code @TableField(fill = ...)}</b>：值交给数据库的
 *       {@code DEFAULT CURRENT_TIMESTAMP(3)} / {@code ON UPDATE CURRENT_TIMESTAMP(3)} 去填。
 *       可行的原因是 MP 默认的字段策略是 NOT_NULL —— 为 null 的字段压根不会出现在 INSERT 语句里，
 *       数据库默认值就有机会生效。代价是 Java 对象里读不到刚写入的时间（本轮只写不读，无所谓）。</li>
 * </ul>
 *
 * <p>实体<b>不出 service 层</b>：{@code password_hash} 这类字段一旦被序列化出去就是事故。
 * 将来要返回给前端的形状放 {@code vo/}（那个包现在还是空的，就是留给它的）。
 */
@Data
@TableName("user")
public class User {

    @TableId(type = IdType.AUTO) // 显式写出来：实体上的声明优先于全局的 id-type 配置 id为主键，数据库自增
    private Long id;

    /** 登录名，唯一。现在恒为 demo */
    private String username;

    /** 昵称，展示用 */
    private String displayName;

    /** ★ 恒为 null。这一列的存在就是「以后加登录」的位置，null 表示该账号还登不了 */
    private String passwordHash;

    private String email;

    /** 1=正常 0=禁用，为登录预留。禁用走这个字段而不是删行 */
    private Integer status;

    private LocalDateTime createdAt;

    private LocalDateTime updatedAt;
}
