package org.example.computerbizservice.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.example.computerbizservice.entity.User;

/**
 * user 表的 mapper。
 *
 * <p><b>空接口是故意的</b>，不是没写完：{@code BaseMapper} 已经把这轮要用的增删改查全给了
 * （insert / updateById / selectById / selectOne ...），单表 DML 一行都不用手写。
 * 这里以后要加方法的时候再写 —— 加一条方法通常意味着「单表表达式不够用了」。
 *
 * <p>★ 注意接口上<b>没有</b> {@code @Mapper}：扫描是在
 * {@code ComputerBizServiceApplication} 上用 {@code @MapperScan} 统一做的。
 * 两条路都走等于把同一个 mapper 注册两次。反过来，只写 {@code @Mapper} 不写
 * {@code @MapperScan} 也能跑，但那样每个 mapper 都要记得加，漏一个就是启动报
 * 「NoSuchBeanDefinitionException: XxxMapper」—— 一处 @MapperScan 更省心。
 */
public interface UserMapper extends BaseMapper<User> {
}
