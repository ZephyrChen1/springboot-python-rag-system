package org.example.computerbizservice.dto.response.rag;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;

@Data
public class FileUploadResp {
    @JsonProperty("job_id")
    private String jobId;

    @JsonProperty("file_name")
    private String fileName;

    private Boolean done; //提交后一般恒为 false
}
