"""待实现：固定 revision 的 Hugging Face 模型接入适配器。

适配器只能从白名单或已审查来源获取模型，默认 ``trust_remote_code=False``。每次采用
必须记录 repository、revision、license、model/config/tokenizer digest、参数规模、
预计资源与 full fine-tuning、冻结 encoder 或 PEFT 策略；先在渐进样本 smoke run，
正式实验只使用已缓存且固定 revision 的本地 artifact。Tabular 任务不默认使用
Transformer；“最佳模型”检索与选择需另行以 rubric 和证据定义。
"""
