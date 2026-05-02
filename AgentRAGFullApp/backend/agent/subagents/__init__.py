"""Sub-agentes especializados para LexAI.

El orquestador (voice agent en OpenAI Realtime) delega tareas complejas a
sub-agentes vía la tool `delegate_to`. Cada sub-agente es un wrapper
sobre `llm_generate` con su propio prompt + subset de tools.

Es ADITIVO: las tools especializadas siguen disponibles directamente al
orquestador para retro-compatibilidad. La delegación es opcional.
"""
