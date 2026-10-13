# ⓿ 一次性准备（MySQL 8.4）
#
# Navicat 里对 root 连接执行一次（密码自己定，别用 root 的）：
#
#   CREATE DATABASE `computer_biz`      DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
#   CREATE DATABASE `computer_biz_test` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;  -- 只有跑 mvn test 才用得上
#   CREATE USER 'computer_biz'@'localhost' IDENTIFIED BY '123456';
#   CREATE USER 'computer_biz'@'127.0.0.1' IDENTIFIED BY '123456';
#   GRANT ALL PRIVILEGES ON `computer_biz`.*      TO 'computer_biz'@'localhost';
#   GRANT ALL PRIVILEGES ON `computer_biz`.*      TO 'computer_biz'@'127.0.0.1';
#   GRANT ALL PRIVILEGES ON `computer_biz_test`.* TO 'computer_biz'@'localhost';
#   GRANT ALL PRIVILEGES ON `computer_biz_test`.* TO 'computer_biz'@'127.0.0.1';
#   FLUSH PRIVILEGES;
#
# 然后建表：db/schema.sql 的全文，在 computer_biz 跑一遍、在 computer_biz_test 再跑一遍。
# （本轮没有迁移工具，以后改表要改这个文件 + 在库上手写 ALTER）

# ① FastAPI（8102）
cd D:\python\code
.\.venv\Scripts\uvicorn.exe rag_api.main:app --host 0.0.0.0 --port 8102 --workers 1

# ② Java BFF（8080）
# 密码不进仓库，每个新终端先设一次。DB_USER 默认就是 computer_biz，不用设
$env:DB_PASSWORD = "123456"
cd D:\java\computer-biz-service
.\mvnw.cmd spring-boot:run

# ③ 前端（5173）
cd D:\java\computer-biz-service\frontend
npm install      # 只有第一次要
npm run dev

# 附：不启动整套服务时用得上的命令
.\mvnw.cmd -B compile      # 只编译，最快的一道检查，不用数据库
.\mvnw.cmd -B test         # 需要 computer_biz_test 库（见 ⓿）
.\mvnw.cmd spring-boot:run "-Dspring-boot.run.arguments=--debug"   # 打自动配置报告，排查「bean 没装配上」

# 启动 ② 之后，日志里按顺序应该出现：HikariPool-1 - Start completed. → 落库自检 ① → 落库自检 ②。
# 卡在哪一步就说明问题在哪一层，详见 docs/落库设计说明.md
