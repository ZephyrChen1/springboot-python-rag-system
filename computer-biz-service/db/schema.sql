-- ============================================================================
-- computer-biz-service 落库层建表脚本（MySQL 8.4 LTS / InnoDB / utf8mb4）
--
-- 【怎么用】在 Navicat 里对 computer_biz 库整段执行，或 mysql -u root -p computer_biz < schema.sql
-- 【前提】库和应用账号已经建好（见 docs/落库设计说明.md，密码不要写进这个文件）
-- 【幂等】否。这是「空库初始化」脚本：重跑会报「表已存在」，那说明你已经跑过了。
--         改表就改这个文件，再在库上手写一条 ALTER（本轮没有迁移工具）
--
-- 【设计取舍】都写在列注释里，理由集中在 docs/落库设计说明.md，这里只说三条最容易被改错的：
--   ① 状态列存**大写枚举名字符串**（'DONE'），不是数字。前端用的是对应的小写值（'done'）。
--   ② error 列要清空时只能写空串 ''，**不能写 null** —— MyBatis-Plus 默认不把 null 字段
--      写进 UPDATE 语句，写 null 等于什么都没发生，上一次的报错会永远留着。
--   ③ 时间列不用 TIMESTAMP：TIMESTAMP 每次读写都要过一遍会话时区、还有 2038 问题。
--      DATETIME + Java 的 LocalDateTime 两边都不转换。
-- ============================================================================


-- ---------------------------------------------------------------- ① 用户
-- 本轮只有一个演示用户（id=1，由 Java 在启动时自动补写，见 common/PersistenceStartupCheck），
-- 但字段按「以后要加登录」留好了。删除账号请改用 status 列，不要删行 ——
-- 删行会让历史上所有 user_id 变成悬空引用。
CREATE TABLE `user` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `username`      VARCHAR(64)  NOT NULL                COMMENT '登录名。现在恒为 demo。唯一键必须现在就建：事后补索引要改数据，现在建是免费的',
  `display_name`  VARCHAR(64)  DEFAULT NULL            COMMENT '昵称，展示用。要把用户名显示给用户时用它，别暴露登录名',
  `password_hash` VARCHAR(100) DEFAULT NULL            COMMENT '★ 恒为 NULL。这一列的存在就是「以后加登录」的位置；用 NULL 而不是空串，是为了让「还没设密码」在 SQL 里一眼可查',
  `email`         VARCHAR(128) DEFAULT NULL,
  `status`        TINYINT      NOT NULL DEFAULT 1      COMMENT '1=正常 0=禁用，为登录预留。禁用走这里而不是删行',
  `created_at`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='用户。本轮只有一条演示数据（id=1），所有业务记录都挂在它下面';


-- ---------------------------------------------------------------- ② 入库任务记录
-- 一次上传一行，轮询时更新这一行（不是追加流水）：上游的轮询接口返回的是**快照**
-- （stage/result/elapsed/done），不是事件。追加流水会为一次入库攒下几百行几乎一样的记录，
-- 而「什么时候结束的」靠 created_at/updated_at + 终态只写一次就够了。
CREATE TABLE `ingest_task` (
  `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `job_id`             VARCHAR(64)   NOT NULL            COMMENT '上游 FastAPI 的 job_id。上游只保留最近一个 job，它天然是幂等键（上游没有幂等键，这个是 schema 能给的全部对账线索）',
  `user_id`            BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '本轮恒为演示用户 1',
  `file_name`          VARCHAR(255)  NOT NULL            COMMENT '上游回显的文件名（resp.file_name）；拿不到时退回 MultipartFile.getOriginalFilename()',
  `original_file_name` VARCHAR(255)  DEFAULT NULL        COMMENT '★ 前端 multipart 里带的原始文件名。与 file_name 分开存：上游可能改名/去空格，事后想知道「用户传的是哪个名字」只有它',
  `file_size`          BIGINT        DEFAULT NULL        COMMENT '字节数（MultipartFile.getSize()）。也用来区分同名的两次上传',
  `content_type`       VARCHAR(128)  DEFAULT NULL        COMMENT '上传时声明的 MIME。排查「上游说格式不支持」时唯一的依据',
  `status`             VARCHAR(16)   NOT NULL            COMMENT 'INGESTING/SUCCESS/SKIPPED/FAILED/UNKNOWN。★ UNKNOWN = 「不知道结果」，**不是失败**：上游只保留最近一个 job，旧 job 的 404 会落在这里；前端也是同一条原则，否则用户会重传导致同一文档入库两次。前端的 queued/uploading/committing 是纯客户端状态，服务端永远看不到',
  `stage`              VARCHAR(255)  DEFAULT NULL        COMMENT '上游的人类可读进度文案，每次轮询覆盖。只留最后一次 —— 它描述的是「当前状态」不是历史，所以列开小一点、用不改写代价低的类型',
  `result`             VARCHAR(512)  DEFAULT NULL        COMMENT '上游 done=true 时的结果文案，前缀是契约：[成功]/[跳过]/[失败]。终态只写一次',
  `elapsed_sec`        DOUBLE        DEFAULT NULL        COMMENT '上游 elapsed_sec（Python float）。这是展示值，不是金额，不需要 DECIMAL',
  `error`              VARCHAR(1024) DEFAULT NULL        COMMENT '★ 一行里除「入库结论」之外要说的话，两类：① 我们没问到的（网络/502/超时）② 上游说了但不是入库结论的（404 的「任务不存在」、result 前缀认不出来）。分界：result 只装上游对「入库这件事」的结论。★ 清空只能写空串，不能写 null（见文件头 ②）',
  `poll_count`         INT           NOT NULL DEFAULT 0  COMMENT '被轮询的次数。「任务卡住了」和「页面早被关了」在库里长得一模一样，只有这个计数能分开',
  `last_polled_at`     DATETIME(3)   DEFAULT NULL        COMMENT '最后一次轮询的时间（成功或失败都算），用来看这行有多旧',
  `created_at`         DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '受理时刻 = 上游返回 job_id 那一刻（文件正在上传的那段，服务端看不到）',
  `updated_at`         DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ingest_job_id` (`job_id`),
  KEY `idx_ingest_user_created` (`user_id`, `created_at`),
  -- 演示用户没建起来时（忘了初始化 / 动过 id），写库当场报约束冲突，
  -- 而不是留下一堆谁也发现不了的悬空 user_id。上面的 (user_id, created_at) 正好满足外键要求的索引
  CONSTRAINT `fk_ingest_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='入库任务记录：一行一个 job，随轮询就地更新（不是流水账）';


-- ---------------------------------------------------------------- ③ 问答会话
-- 前端每次刷新页面 / 点「重新开始」都会 newSessionId()，所以会话短而多。
CREATE TABLE `chat_session` (
  `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `session_id` VARCHAR(128)    NOT NULL              COMMENT '前端 crypto.randomUUID() 生成的会话号。上游要求 1~128 位、只允许字母/数字/下划线/连字符',
  `user_id`    BIGINT UNSIGNED NOT NULL DEFAULT 1    COMMENT '本轮恒为演示用户 1',
  `title`      VARCHAR(64)     DEFAULT NULL          COMMENT '会话标题：取该会话第一句提问的前若干字，建会话时顺手填一次，纯展示用。将来做会话列表时不用再回头解析消息',
  `created_at` DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_chat_session_id` (`session_id`),
  CONSTRAINT `fk_session_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='问答会话';


-- ---------------------------------------------------------------- ④ 问答消息
-- 一问一答各一行。助手那行的正文是 Java 代理 SSE 时自己攒的 ——
-- 上游只会把答案流给我们，不会在别处留一份。
CREATE TABLE `chat_message` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `session_id`  VARCHAR(128)    NOT NULL              COMMENT '指向 chat_session.session_id。这条**刻意不加外键**：会话行和它的子行是在同一个事务里创建的，约束只能抓到我们自己的 bug；user_id 那条能抓到「初始化没跑」这种外部状态，所以那条加了',
  `user_id`     BIGINT UNSIGNED NOT NULL DEFAULT 1    COMMENT '本轮恒为演示用户 1。刻意冗余在每一行上：以后「我的历史」是最常查的一句，冗余一个 BIGINT 换掉一次 join 是划算的',
  `role`        VARCHAR(16)     NOT NULL              COMMENT 'user / assistant，取值与前端 chatStore 一致',
  `content`     MEDIUMTEXT      DEFAULT NULL          COMMENT 'user 行 = 提问原文；assistant 行 = 已收到的回答（可能只有半截，也可能还没开始）。★ 存的是**上游原始流文本**，与用户屏幕上的未必一致：前端会丢掉它不认识的 JSON 帧（如 sources）。在 Java 侧再实现一遍那个过滤 = 制造第二份前端契约，正是本项目一贯拒绝的事（错误翻译「只能有一份实现」同理）。真要过滤，就加一列存解析后的，别覆盖这列。MEDIUMTEXT 上限 16MB，Java 侧另有 10 万字符的攒取上限',
  `status`      VARCHAR(16)     NOT NULL              COMMENT 'STREAMING/DONE/CANCELED/INTERRUPTED/ERROR，与前端 chatStore.MSG 逐条对应，别自己造第六个。CANCELED=用户点了「停止生成」（不是故障）；INTERRUPTED=收到一部分但流没正常结束；ERROR=一个字都没收到就失败了',
  `error`       VARCHAR(1024)   DEFAULT NULL          COMMENT 'ERROR/INTERRUPTED 时的原因；DONE 和 CANCELED 时是空串（不是 null —— MP 会跳过 null 字段，那样清不掉旧值）',
  `top_k`       INT             DEFAULT NULL          COMMENT '本次检索条数（上游限 1~100），只写在 user 行。将来解释「同一个问题两次答案差很远」时，参数不同是第一个嫌疑',
  `char_count`  INT             DEFAULT NULL          COMMENT '助手正文的字符数。一眼看出哪些回答是半截的（取消/中断）',
  `created_at`  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '★ 同一轮问答的 user/assistant 两行用**同一个值**（对齐前端「两条消息共享 createdAt」的语义），所以一轮内的先后只能靠 id 排，不能靠时间',
  `finished_at` DATETIME(3)     DEFAULT NULL          COMMENT '终态时间；STREAMING 时是 NULL。两个用途：捞「卡住的流」（status=STREAMING AND finished_at IS NULL），以及开机清扫僵尸流时改它',
  PRIMARY KEY (`id`),
  KEY `idx_msg_session_id` (`session_id`, `id`),
  KEY `idx_msg_user_created` (`user_id`, `created_at`),
  CONSTRAINT `fk_msg_user` FOREIGN KEY (`user_id`) REFERENCES `user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='问答消息：一次问答两行（user + assistant），助手行先以 STREAMING 建出来、终态时更新一次';
