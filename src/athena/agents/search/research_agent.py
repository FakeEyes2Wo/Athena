"""待实现：为假设生成提供证据的研究 Agent。

Agent 通过 RAG 检索相关领域论文、博客和技术报告；PDF 在索引或推理前须转换为
Markdown。它把支持与反对材料记录为 artifact，生成可证伪的 Hypothesis 节点并接入
HypoTree；检索到的 Hugging Face 候选模型只作为研究证据，后续采用仍受来源审查、
固定 revision 和资源约束控制。
"""
