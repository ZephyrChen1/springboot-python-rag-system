-- MySQL 官方镜像只在数据目录为空时执行 /docker-entrypoint-initdb.d/ 下的脚本。
-- entrypoint 不会自动 USE MYSQL_DATABASE，所以这里显式选库；
-- 然后用 SOURCE 复用 computer-biz-service/db/schema.sql，不维护第二份 schema。
--
-- 改表时仍然改 schema.sql；已经初始化过的卷不会重新执行本脚本，
-- 需要手动 ALTER，或者 docker compose down -v 重建（会丢数据）。
USE `computer_biz`;
SOURCE /schema/schema.sql;
