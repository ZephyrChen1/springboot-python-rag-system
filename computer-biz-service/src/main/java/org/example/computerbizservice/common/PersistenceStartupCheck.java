package org.example.computerbizservice.common;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.computerbizservice.common.enums.ChatMsgStatus;
import org.example.computerbizservice.entity.User;
import org.example.computerbizservice.mapper.UserMapper;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

// SpringBoot 项目启动时自动执行的数据库层自检组件

/**
 * 落库层的启动自检。干三件事，顺序就是它们的依赖顺序：
 *
 * <ol>
 *   <li><b>证明「库连上了」</b> —— 用 {@link JdbcTemplate} 查一次版本号。这一步<b>完全不碰 MyBatis</b>，
 *       所以它能和下一步把两类故障分开。</li>
 *   <li><b>证明「ORM 接上了」</b> —— 用 mapper 查一次演示用户。这一步走的是
 *       SqlSessionFactory → 语句生成 → 下划线转驼峰 这条完整的路。
 *       <b>①打印了、②炸了 ⇒ 是 MyBatis 的接线问题，不是 MySQL 的问题</b> —— 这一句能省掉半小时。</li>
 *   <li><b>补写演示用户 + 清扫僵尸流</b>（见下面各自的方法）。</li>
 * </ol>
 *
 * <p>为什么自检要放在启动器里而不是做成一个 /health 接口：本轮只写不读，没有接口给它挂；
 * 而且「启动就自报家门」比「等你想到去调它」可靠。
 *
 * <p>注意它<b>不捕获</b>①的异常：库不存在 / 密码不对 / MySQL 没起来，都会让启动直接失败
 * （Hikari 启动即建连，本来也会失败），日志里那句话就是答案。这正是想要的 —— 缺库要吵。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class PersistenceStartupCheck implements ApplicationRunner {

    /** 僵尸流的收尾说明。要和 docs/落库设计说明.md 里的巡检 SQL 对得上 */
    private static final String ZOMBIE_STREAM_ERROR = "Java 进程在生成过程中退出（启动自检发现）";

    private static final String DEMO_USERNAME = "demo";
    private static final String DEMO_DISPLAY_NAME = "演示用户";

    private final JdbcTemplate jdbcTemplate;
    private final UserMapper userMapper;

    @Override
    public void run(ApplicationArguments args) {
        checkConnection();
        ensureDemoUser();
        sweepZombieStreams();
    } // 数据库层自检

    /** ① 只证明连接可用。失败就让它抛 —— 启动阶段失败比运行时失败好排查得多 */
    private void checkConnection() {
        String version = jdbcTemplate.queryForObject("SELECT VERSION()", String.class);
        log.info("落库自检 ①：数据库已连通（MySQL {}）。接下来这行若没出现，问题在 MyBatis 而不是 MySQL", version);
    }

    /**
     * ② 演示用户：不在就补一行。
     *
     * <p>为什么由代码补而不是写进 db/schema.sql：见 AppConstants.DEFAULT_USER_ID —— 本轮没有登录，
     * 这个身份是「服务端替用户声明」的，属于运行期约束，属于代码。附带好处是你手工建库时
     * 少一条要记得执行的语句。
     *
     * <p>两种失败要区别对待，这是这个方法唯一需要小心的地方：
     * <ul>
     *   <li><b>查询本身就炸了</b>（表不存在 / 列名对不上 / 权限不够）→ <b>让它抛，启动失败</b>。
     *       这是「ORM 接线通没通」的答案，也是 §①打印了、这里炸了 = 问题在 MyBatis 不在 MySQL
     *       那句话成立的前提。而它最常见的成因就是「忘了跑 db/schema.sql」—— 那时候起不来才是对的。</li>
     *   <li><b>查到了「没有这个用户」、但补写失败</b> → <b>吞掉，只打 ERROR</b>。
     *       说明表和接线都是好的，坏的只是这一次写。问答和上传本身还能用
     *       （只是落库会撞 user_id 的外键而写不进去，每次都会打 ERROR 日志），
     *       这时候一个能跑的服务比一个起不来的服务有用 —— 这就是「落库是旁路」。</li>
     * </ul>
     */
    private void ensureDemoUser() {
        User demo = userMapper.selectById(AppConstants.DEFAULT_USER_ID);
        if (demo != null) {
            log.info("落库自检 ②：MyBatis-Plus 接线正常（演示用户 id={} username={}）",
                    demo.getId(), demo.getUsername());
            return;
        }

        User fresh = new User();
        fresh.setId(AppConstants.DEFAULT_USER_ID); // 显式给 id，让「常量 1」这件事在库里是确定的
        fresh.setUsername(DEMO_USERNAME);
        fresh.setDisplayName(DEMO_DISPLAY_NAME);
        fresh.setStatus(1);
        // password_hash 故意不写：null 表示这个账号还登不了（本轮没有登录，见 user 表的列注释）
        try {
            userMapper.insert(fresh);
        } catch (Exception e) {
            log.error("落库自检 ②：演示用户 id={} 不存在且补写失败（status=1 的插入被拒）。"
                    + "服务照常启动，但落库会因 user_id 外键冲突而写不进去"
                    + "（那条路只打日志，不影响问答/上传）。",
                    AppConstants.DEFAULT_USER_ID, e);
            return;
        }
        log.info("落库自检 ②：user 表里没有 id={} 的演示用户，已补写（id 回填为 {}，password_hash 保持 null）",
                AppConstants.DEFAULT_USER_ID, fresh.getId());
    }

    /**
     * ③ 清扫僵尸流：上次进程退出时还停在 STREAMING 的助手行，开机时全部改成 INTERRUPTED。
     *
     * <p>凭什么能确定它们是僵尸：一条流不可能活过一次进程重启。所以开机时
     * {@code status='STREAMING'} 的行<b>必然</b>是「答案没落下来」的残骸。
     *
     * <p>这就是「助手行先建（STREAMING）后改」多花的那一次写换来的东西 —— 崩了也看得出哪条追问
     * 没有答案，而不是让它永远挂在 STREAMING 上，和「正在生成」混在一起分不清。
     *
     * <p>用 JdbcTemplate 而不是 mapper：这条语句要能在 ORM 出问题时照跑，
     * 它属于「诊断/清理」，越少依赖越好。取值从枚举的 code 拿，避免字面量写歪。
     *
     * <p>失败<b>吞掉只打 ERROR</b>：它是一次维护，不是服务能干活的前提（和 ① 相反）。
     * 为了「清扫失败」而不让服务起来，是把旁路当成了主链路。
     */
    private void sweepZombieStreams() {
        try {
            int n = jdbcTemplate.update(
                    "UPDATE chat_message SET status = ?, error = ?, finished_at = NOW(3) WHERE status = ?",
                    ChatMsgStatus.INTERRUPTED.getCode(),
                    ZOMBIE_STREAM_ERROR,
                    ChatMsgStatus.STREAMING.getCode());
            if (n > 0) {
                log.warn("落库自检 ③：清扫了 {} 条上次进程留下的僵尸流（STREAMING → INTERRUPTED）", n);
            }
        } catch (Exception e) {
            log.error("落库自检 ③：僵尸流清扫失败。服务照常启动，"
                    + "但库里可能残留 status='STREAMING' 的历史行（那只是显示不准，不影响写入）。", e);
        }
    }
}
