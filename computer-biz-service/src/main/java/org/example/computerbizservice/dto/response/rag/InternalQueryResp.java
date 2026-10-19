package org.example.computerbizservice.dto.response.rag;

import lombok.Getter;
import lombok.RequiredArgsConstructor;

import java.util.List;

/**
 * 内部查询的统一响应。
 *
 * <p>没有翻页：agent 工具只做「看一眼现状」这种小查询，每条查询都在服务端强制
 * {@code LIMIT 1..100}。真要做导出/分页，那应该是另一个接口、另一套鉴权。
 */
@Getter
@RequiredArgsConstructor
public class InternalQueryResp {

    private final String query;

    private final int count;

    private final List<?> items;
}
