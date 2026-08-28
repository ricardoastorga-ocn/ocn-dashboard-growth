# Dashboard - Growth (OCN)

Dashboard operativo de flota y waitlist de OCN. Se refresca solo, varias veces al día en horario
de oficina, cada 2 horas (8am/10am/12pm/2pm/4pm/6pm, hora CDMX, lunes a viernes) via GitHub Actions — sin costo,
sin un Claude corriendo cada vez.

## Cómo funciona

- `refresh_data.py`: pull a Google Sheets (Back Office + Presales-Inventory), calcula todo
  (mix, modelo, embudo, waitlist por tier/ciudad/agente, motivos de pérdida, tiempos de entrega),
  y escribe `data.js`.
- `index.html`: la página del dashboard. Lee `data.js` en el navegador — nunca se edita a mano.
- `.github/workflows/refresh.yml`: corre `refresh_data.py` en el horario de arriba, y si hay
  cambios hace commit + push de `data.js`. Vercel redespliega solo en cada push (sin cron propio
  de Vercel, sin plan de paga).

## Refresh manual / urgente

Sin esperar el horario: `gh workflow run refresh.yml` (o desde la pestaña Actions en GitHub,
botón "Run workflow").

## Credenciales

El script necesita 3 variables de entorno (ya cargadas como GitHub Secrets en este repo):
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`. Mismo refresh token de
Google que ya se usa para los demás reportes de OCN — no requiere volver a autenticar.
