# AI Anomaly Detector

Componente Python per il rilevamento di anomalie nei dati del sistema **Train Ticket**.

Analizza pattern anomali negli ordini, prenotazioni e comportamenti degli utenti
usando tecniche di machine learning e analisi statistica.

## Struttura

```
ai-anomaly-detector/
├── src/          # Codice Python (moduli, classi, pipeline)
├── notebooks/    # Jupyter notebook per analisi ed esperimenti
├── data/         # Dati di esempio e dataset per il training
└── README.md     # Questo file
```

## Cosa fa

- Rileva anomalie negli ordini (cancellazioni anomale, picchi di traffico, pattern sospetti)
- Analizza i log di chiamate tra i microservizi di Train Ticket
- Supporta analisi offline su dati storici e integrazione con i servizi tramite API

## Stack

- Python 3.x
- Jupyter per esplorazione dati
- Dati provenienti dai microservizi Train Ticket (ordini, prenotazioni, pagamenti)
