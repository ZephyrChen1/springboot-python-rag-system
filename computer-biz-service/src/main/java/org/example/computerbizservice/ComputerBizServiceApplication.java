package org.example.computerbizservice;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;


@SpringBootApplication
@MapperScan("org.example.computerbizservice.mapper") //告诉 MyBatis‑Plus：去哪个包下面扫描 Mapper 接口，自动把这些接口注册成 Spring 的 Bean，就能直接 `@Autowired` 注入使用
//这样就不用在每个接口都写 @Mapper
public class ComputerBizServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(ComputerBizServiceApplication.class, args);
    }

}
