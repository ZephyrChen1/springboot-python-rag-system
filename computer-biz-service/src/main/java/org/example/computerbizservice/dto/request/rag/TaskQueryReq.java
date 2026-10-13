package org.example.computerbizservice.dto.request.rag;

import lombok.Data;
import jakarta.validation.constraints.NotBlank;
//import org.springframework.web.bind.annotation.PathVariable;

@Data
public class TaskQueryReq {
    @NotBlank(message = "jobId不能为空") //json请求体才需要jsonproperty
    private String jobId;
}
