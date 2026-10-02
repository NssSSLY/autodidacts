from autodidact import models


async def conditional_claim(session, dispute, incoming, claim_id):
    if claim_id is None:
        return incoming
    candidate = await session.get(models.Claim, claim_id)
    investigated = (dispute.resolution_metadata or {}).get("investigation_session_ids", [])
    if candidate is None or str(candidate.learning_session_id) not in investigated:
        raise ValueError("条件结论必须来自此争议已记录的调查会话")
    return candidate
