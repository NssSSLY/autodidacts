# 文件职责：限制条件化结论必须来自对应争议已记录的调查会话。
from autodidact import models


# 功能：读取条件结论 Claim 并检查调查关联；未给 ID 时沿兼容路径使用 incoming，非法 ID 拒绝决议。
async def conditional_claim(session, dispute, incoming, claim_id):
    if claim_id is None:
        return incoming
    candidate = await session.get(models.Claim, claim_id)
    investigated = (dispute.resolution_metadata or {}).get("investigation_session_ids", [])
    if candidate is None or str(candidate.learning_session_id) not in investigated:
        raise ValueError("条件结论必须来自此争议已记录的调查会话")
    return candidate
