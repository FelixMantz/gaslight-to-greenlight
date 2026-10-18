from inspect_ai.model import modelapi


@modelapi(name="g2g")
def g2g():
    from g2g.inspect_tasks.provider import JournalModelAPI

    return JournalModelAPI
