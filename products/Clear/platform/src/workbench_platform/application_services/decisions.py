"""Fixed user decisions shared by typed business operations and Agent tools."""
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentToolResult


class AskUser(AgentModel):
    question: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=10)


def ask_user(args, ctx):
    with ctx.store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO decisions(id,run_id,question,revision) VALUES(?,?,?,1)',
                   (ctx.operation_id, ctx.run_id, args.question))
    return AgentToolResult(status='needs_user', summary=args.question,
        data={'decision_id': ctx.operation_id, 'revision': 1, 'options': args.options}).model_dump()
