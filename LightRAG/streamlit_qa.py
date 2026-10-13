import streamlit as st
import time

import config_data as config
from config_data import session_config
from rag import RagService

#缺 DASHSCOPE_API_KEY 时直接报错退出
config.validate_config(strict=True)

#标题
st.title("计算机知识专家")
st.divider() # 分隔符

#保留上下文
if "message" not in st.session_state:
    st.session_state["message"] = [{"role": "assistant", "content": "你好，请问有什么可以帮到您"}]
#rag对象
if "rag" not in st.session_state:
    st.session_state["rag"] = RagService()

#显示上下文
for message in st.session_state["message"]:
    st.chat_message(message["role"]).write(message["content"])

#提供用户输入栏
prompt = st.chat_input()

if prompt:
    # 渲染一个聊天框
    st.chat_message("user").write(prompt)
    st.session_state["message"].append({"role": "user", "content": prompt})

    ai_res_list = []  # 存放因流式输出而没掉的输出结果

    with st.spinner("AI思考中。。。"):

        res_stream = st.session_state["rag"].stream(prompt, session_config) #RagService().stream会返回一个生成器generator（yield的作用）

        def capture(generator, cache_list):
            for chunk in generator:
                cache_list.append(chunk)
                yield chunk
        #captrue是为了保存流式输出的结果

        st.chat_message("assistant").write_stream(capture(res_stream, ai_res_list))

        # 用流式输出的话，正常的话上下文就保存不到了
        st.session_state["message"].append({"role": "assistant", "content": "".join(ai_res_list)})  # 快捷把列表转为str