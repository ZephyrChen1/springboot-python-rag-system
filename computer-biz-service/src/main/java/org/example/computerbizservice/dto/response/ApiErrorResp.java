package org.example.computerbizservice.dto.response;

import lombok.Data;
import lombok.AllArgsConstructor;

/** FastAPI 统一错误体：{"detail": "..."}，401/404/409/422 都是这个结构 */
@Data
@AllArgsConstructor
public class ApiErrorResp {
    private String detail;
}