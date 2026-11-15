from pydantic import BaseModel, Field, field_validator


class SampleIn(BaseModel):
    # 两值必填且非空（与旧表 NOT NULL 约束一致）
    temp: float = Field(allow_inf_nan=False)
    ph: float = Field(allow_inf_nan=False)


class SampleOut(BaseModel):
    id: int
    temp: float
    ph: float


class CalibrationIn(BaseModel):
    """温度两点校正单：校正单号 + 两对（原始温度, 标准温度）+ 样本编号列表。"""

    order_no: str = Field(min_length=1, max_length=64)
    raw1: float = Field(allow_inf_nan=False)
    std1: float = Field(allow_inf_nan=False)
    raw2: float = Field(allow_inf_nan=False)
    std2: float = Field(allow_inf_nan=False)
    sample_ids: list[int] = Field(min_length=1, max_length=20)

    @field_validator("order_no")
    @classmethod
    def _strip_order_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("校正单号不能为空")
        return v

    @field_validator("sample_ids")
    @classmethod
    def _positive_ids(cls, v: list[int]) -> list[int]:
        if any(sid <= 0 for sid in v):
            raise ValueError("样本编号必须为正整数")
        return v
