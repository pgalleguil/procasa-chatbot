# PROCASA chatbot stability simulation

Offline-only report. Synthetic data, mongomock and provider/LLM fakes; no production database, Wasender or DeepSeek calls.

- Generated at UTC: `2026-09-16T01:57:45.156131+00:00`
- Baseline/live reference: `7cb4ff304483b3b71a8c0c340ec2dcd9a07fd7e4`
- Total scripted scenarios: `200`
- Total scripted inbound turns: `255`
- Stress: `50` conversations / `500` inbounds
- Pass rate: `100%`

## Gates

- `STALE_RESPONSES_SENT` = `0`
- `DUPLICATE_OUTBOUNDS` = `0`
- `CROSS_CONVERSATION_LEAKS` = `0`
- `HALLUCINATED_PROPERTY_FACTS` = `0`
- `WRONG_PROPERTY_MATCHES` = `0`
- `MISSED_HANDOFFS` = `0`
- `WRONG_EXECUTIVE_ASSIGNMENTS` = `0`
- `ACK_WRONG_REPLIES` = `0`
- `GENERIC_FALLBACKS` = `0`
- `UNHANDLED_429` = `0`

## Representative transcripts

All messages below are synthetic and contain no customer PII.

### 1. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 2. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 3. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 4. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 5. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 6. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 7. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 8. greeting_general
- **CUSTOMER**: Hola, ¿cómo están?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 9. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 10. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 11. greeting_general
- **CUSTOMER**: Hola, ¿cómo están?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 12. greeting_general
- **CUSTOMER**: Hola, ¿cómo están?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 13. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 14. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 15. greeting_general
- **CUSTOMER**: Hola, ¿cómo están?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 16. greeting_general
- **CUSTOMER**: Busco información
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 17. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 18. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 19. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 20. greeting_general
- **CUSTOMER**: Hola
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 21. property_specific
- **CUSTOMER**: ¿Tiene estacionamiento?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 22. property_specific
- **CUSTOMER**: ¿Tiene estacionamiento?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 23. property_specific
- **CUSTOMER**: ¿Cuántos dormitorios tiene?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 24. property_specific
- **CUSTOMER**: ¿Cuál es la orientación?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded

### 25. property_specific
- **CUSTOMER**: ¿Cuántos dormitorios tiene?
- **BOT**: [synthetic] resultado=1 outbound; estado=responded
