"""Robin Egg Server, MVP: the Mac mini side of the Robin Egg.

It speaks ESPHome's native API to the egg running the stock formatBCE firmware,
so no Home Assistant is involved. One turn is:

    wake word on the egg -> mic audio -> speech-to-text -> LLM reply
    -> text-to-speech, streamed sentence by sentence -> the egg's speaker
"""

__version__ = "0.1.0"
