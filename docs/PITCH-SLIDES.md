# AWS Transform: una transformación, tres stacks

> Guion de diapositivas · 30 min de charla + 15 min de demo · Español · Formato keynote con animaciones
> Los tres stacks son **Legacy → Agentes → Target**:
> 1. **Legacy:** EC2 + Flask + SQLite.
> 2. **Agentes:** Amazon Bedrock AgentCore + Strands + Nova/DeepSeek.
> 3. **Target:** Kubernetes + Terraform + Kustomize.

Este documento es la fuente para la plantilla. Por cada diapositiva trae:
- **objetivo**
- **en pantalla** (el texto exacto)
- **animaciones** (qué elemento, cuándo y con qué tipo)
- **sin animación** (estado final estático)
- **notas del orador**
- **transición**

Los tipos de animación se definen una sola vez en la sección 1. La plantilla implementa cada tipo y aquí solo se referencian
por nombre con sus parámetros.

Las cifras salen de la ejecución grabada (`samples/recorded-run`). Para refrescarlas, ejecuta `make numbers` antes de
cada charla.

---

## 0. Mapa de tiempos

| # | Diapositiva | Min |
|---|---|---|
| 1 | Portada | 0:30 |
| 2 | ¿Quién soy? | 1:00 |
| 3 | El servidor que nadie quiere tocar | 2:00 |
| 4 | El problema no es la tecnología | 2:00 |
| 5 | ¿Qué es AWS Transform? | 3:00 |
| 6 | Cómo trabaja: el loop con humano | 2:00 |
| 7 | ¿Por qué un mini-demo? | 1:30 |
| 8 | Una transformación, tres stacks | 2:00 |
| 9–14 | Los seis agentes (1:30 c/u) | 9:00 |
| 15 | Cinco lecciones | 3:00 |
| 16 | ¿Por qué AWS Transform importa? | 2:30 |
| 17 | Demo en vivo (transición) | 0:30 |
| — | **Demo** (la app del repositorio) | 15:00 |
| 18 | Gracias + 3 QR | Q&A |

---

## 1. Sistema de animación (implementar una vez en la plantilla)

### 1.1 Disparadores

- `entrar`: al llegar a la diapositiva.
- `clic N`: el N-ésimo avance del presentador dentro de la diapositiva (builds).
- `loop`: animación continua mientras la diapositiva está visible.

### 1.2 Curvas (easing)

| Nombre | Curva | Uso |
|---|---|---|
| `suave` | `cubic-bezier(.22,1,.36,1)` | entradas |
| `firme` | `cubic-bezier(.65,0,.35,1)` | movimientos de un punto a otro |
| `rebote` | `cubic-bezier(.34,1.56,.64,1)` | sellos y badges (máx. 1 por diapositiva) |

### 1.3 Vocabulario de animaciones

Duraciones por defecto entre paréntesis. Cada tipo tiene su versión "sin animación".

| Tipo | Qué hace | Por defecto | Sin animación |
|---|---|---|---|
| `fade-up` | Opacidad 0→1 y sube 24px | 600ms, `suave` | Aparece en su posición final |
| `stagger` | Aplica el tipo indicado a los hijos uno por uno | 90ms entre hijos | Todos visibles a la vez |
| `split-words` | El texto entra palabra por palabra (fade-up 12px + blur 6px→0) | 70ms por palabra | Texto completo |
| `scale-in` | Escala 0.92→1 con fade | 500ms, `suave` | Tamaño final |
| `blur-in` | Desenfoque 12px→0 con fade | 700ms | Nítido |
| `draw-line` | Dibuja un trazo SVG (stroke-dashoffset) | 900ms, `firme` | Trazo completo |
| `count-up` | Cuenta desde 0 hasta el valor; dígitos tabulares | 1200ms, `suave` | Valor final |
| `typewriter` | Escribe el texto carácter a carácter con un cursor | 28ms por carácter | Texto completo, sin cursor |
| `highlight-sweep` | Un resaltado barre la línea de izquierda a derecha | 500ms | Resaltado fijo |
| `stamp` | Sello que cae con escala 1.4→1 y un pequeño rebote | 450ms, `rebote` | Sello fijo |
| `stack-build` | Capas que suben y se apilan de abajo hacia arriba | 120ms entre capas | Pila completa |
| `morph` | Un elemento se transforma en otro (posición, tamaño, forma) | 800ms, `firme` | Estado final |
| `flow` | Partículas que viajan por un trazo SVG | `loop`, 3s por recorrido | Trazo con flechas fijas |
| `radar` | Barrido circular sobre un área | `loop`, 2.4s por vuelta | Sin barrido |
| `pulse` | Halo suave que respira alrededor de un elemento | `loop`, 2.8s | Halo fijo tenue |
| `drift` | Fondo con manchas de color que se desplazan lento | `loop`, 20s | Fondo estático |
| `crack-rebuild` | Un bloque se fractura en piezas que se reordenan en otra forma | 1400ms | Forma final |
| `grow-bar` | Barras que crecen desde la base | 900ms, `suave` | Altura final |
| `flip-in` | Tarjeta que gira en Y de 90°→0° | 600ms | Tarjeta de frente |
| `shake-soft` | Vibración leve (±3px, 3 ciclos), solo para "error" | 300ms | Sin vibración |

### 1.4 Botón "Pausar animaciones" (obligatorio)

- **Ubicación:** fijo abajo a la derecha, 40×40px, discreto (icono de pausa con trazo fino, 60% de opacidad, 100% al
  pasar el cursor). Tooltip: "Pausar animaciones (P)".
- **Atajo:** tecla **P**. El icono pasa a "reproducir" y el tooltip a "Reanudar animaciones (P)".
- **Efecto al pausar:**
  - todo salta a su estado final ("sin animación");
  - se detienen todos los `loop` (`flow`, `radar`, `pulse`, `drift`);
  - las transiciones entre diapositivas pasan a un fundido simple de 150ms;
  - los builds `clic N` siguen funcionando, pero lo que aparece en cada clic lo hace sin movimiento.
- **Persistencia:** recordar el estado entre diapositivas y recargas (localStorage). Arrancar en pausa si el sistema
  tiene `prefers-reduced-motion: reduce`.

### 1.5 Reglas para proyector

- Trazos de al menos 2px a 1080p. Nada de líneas finísimas, porque desaparecen en proyectores.
- Sin destellos a pantalla completa. `shake-soft` es el único efecto de "error", y nunca más de 3 ciclos.
- Como máximo tres puntos animados simultáneos por paso. Las entradas no deben superar los 900ms.
- Los `loop` deben ser lentos y de baja amplitud: que acompañen sin distraer.
- Probar en 1280×720 con el brillo del proyector: alto contraste, texto mínimo de 24px en pantalla.

### 1.6 Transición entre diapositivas

Por defecto, fundido con un leve desplazamiento (300ms, `suave`). Las excepciones se indican en cada diapositiva.

---

## 2. Diapositivas

### Diapositiva 1. Portada

**Objetivo:** abrir con impacto y plantar la idea de los tres stacks.

**En pantalla**
- Título: **AWS Transform**
- Subtítulo: **una transformación, tres stacks**
- Franja inferior con tres bloques unidos por flechas: **Legacy** → **Agentes** → **Target**
- Pie: Santiago Castellanos · AWS Community Builder

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Fondo de manchas de color | `loop` | `drift` | 20s, amplitud baja |
| "AWS Transform" | `entrar` | `split-words` | +0ms |
| "una transformación, tres stacks" | `entrar` | `blur-in` | +500ms |
| Bloques Legacy / Agentes / Target | `entrar` | `stagger` de `scale-in` | +900ms, 150ms entre bloques |
| Flechas entre bloques | `entrar` | `draw-line` | +1300ms |
| Flechas | `loop` | `flow` | Partículas de izquierda a derecha |
| Pie | `entrar` | `fade-up` | +1600ms |

**Sin animación:** todo visible y las flechas estáticas.

**Notas del orador:**
> Buenas tardes. Hoy vamos a abrir una caja negra: AWS Transform. Y lo vamos a hacer recorriendo una sola
> transformación a través de tres stacks: de dónde venimos, quién hace el trabajo y a dónde llegamos.

**Transición:** fundido.

---

### Diapositiva 2. ¿Quién soy?

**Objetivo:** presentarte con calidez en un minuto.

**En pantalla**
- Foto (círculo) y el nombre **Santiago Castellanos**
- Sr. Cloud Engineering Manager · AWS Solutions Leader en **EPAM**
- Líder de las comunidades **CloudCrafters** y **AI AWS UG Colombia**
- **AWS Community Builder** · **MBA**
- Línea final, más cálida: *Papá de José María y esposo de María Lucía*

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Foto | `entrar` | `scale-in` | 500ms |
| Anillo alrededor de la foto | `entrar` | `draw-line` | +200ms, 1200ms |
| Nombre | `entrar` | `split-words` | +300ms |
| Rol y empresa | `entrar` | `fade-up` | +700ms |
| Badges (CloudCrafters, AI AWS UG Colombia, Community Builder, MBA) | `entrar` | `stagger` de `flip-in` | +1000ms, 120ms entre badges |
| Línea familiar | `clic 1` | `blur-in` | 900ms, con un pequeño icono de corazón dibujado con `draw-line` |

**Sin animación:** todo visible. La línea familiar aparece en el clic 1, sin movimiento.

**Notas del orador:**
> Soy Santiago. Lidero ingeniería cloud y la práctica de AWS en EPAM, y junto a muchos de ustedes hacemos comunidad en
> CloudCrafters y en AI AWS UG Colombia.
> *(clic)* Y lo más importante: papá de José María y esposo de María Lucía, que me prestaron esta tarde para estar acá.

**Transición:** fundido.

---

### Diapositiva 3. El servidor que nadie quiere tocar

**Objetivo:** enganchar con un dolor que todos reconocen.

**En pantalla**
- Arriba, gigante: **"Nadie quiere tocar ese servidor."**
- En el centro, el icono de un servidor con una etiqueta: `desplegado en 2017 · a mano · "temporal"`
- Contador: **años sin tocarlo:** 9
- Tres etiquetas alrededor que aparecen por clic:
  - "La contraseña está en el código"
  - "No hay staging"
  - "Un cron llena el disco cada noche"

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Frase principal | `entrar` | `typewriter` | 28ms por carácter |
| Servidor | `entrar` | `scale-in` | +600ms |
| Polvo sobre el servidor (partículas lentas) | `loop` | `drift` | partículas, 12s |
| Contador "años sin tocarlo" | `entrar` | `count-up` | +900ms, de 0 a 9 |
| Etiqueta 1 | `clic 1` | `fade-up` + `shake-soft` del servidor | — |
| Etiqueta 2 | `clic 2` | `fade-up` + `shake-soft` | — |
| Etiqueta 3 | `clic 3` | `fade-up` + `shake-soft` | — |

**Sin animación:** el servidor y la frase visibles, con el contador en 9. Las etiquetas aparecen por clic, sin vibración.

**Notas del orador:**
> Levanten la mano si en su empresa hay un servidor que nadie quiere tocar. *(pausa)*
> Déjenla arriba si además es "temporal". *(clic)* La contraseña está en el código. *(clic)* No hay staging.
> *(clic)* Y un cron escribe un reporte cada noche en un disco que se va llenando.
> Todos conocemos ese servidor. Hoy lo vamos a migrar.

**Transición:** el servidor hace un `morph` hacia la izquierda y se encoge para la diapositiva 4.

---

### Diapositiva 4. El problema no es la tecnología

**Objetivo:** reencuadrar: las migraciones fallan por conocimiento, no por código.

**En pantalla**
- Título: **Las migraciones no fallan por el código. Fallan por el conocimiento.**
- Lista de preguntas (por clic):
  - ¿Qué hace realmente esta aplicación?
  - ¿De qué depende?
  - ¿Dónde están los secretos?
  - ¿Qué se rompe si la muevo?
  - ¿Cuánto va a costar el mundo nuevo?
- Gráfica pequeña a la derecha: **deuda de migración** (una línea que sube) frente a **capacidad del equipo** (una línea
  plana)

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Título | `entrar` | `split-words` | La palabra "conocimiento" con `highlight-sweep` al final |
| Preguntas | `clic 1..5` | `fade-up`, una por clic | — |
| Línea de capacidad | `clic 5` | `draw-line` | 600ms |
| Línea de deuda | `clic 5` | `draw-line` | 1200ms, con un `pulse` al llegar arriba |

**Sin animación:** título y gráfica visibles; las preguntas aparecen por clic.

**Notas del orador:**
> El destino no es el problema: tenemos contenedores, Kubernetes y Terraform. El problema es el viaje.
> Antes de escribir una sola línea de infraestructura alguien tiene que responder estas preguntas…
> *(clics)* …por cada aplicación del portafolio. Eso es arqueología, análisis y traducción. Por eso la deuda de
> migración crece más rápido que los equipos.

**Transición:** fundido.

---

### Diapositiva 5. ¿Qué es AWS Transform?

**Objetivo:** explicar el servicio real y su alcance, sin cifras que no podamos respaldar.

**En pantalla**
- Centro: **AWS Transform** · *agentes de IA para migración y modernización*
- Cinco tarjetas en órbita (aparecen por clic):
  1. **Assessment:** del inventario al caso de negocio y el TCO.
  2. **Migración (VMware y on-premises):** descubrimiento, agrupación por dependencias, oleadas, red como IaC (incluye
     Terraform), rehost y contenedores hacia ECS o EKS.
  3. **Windows y .NET:** de .NET Framework a .NET moderno; de SQL Server a Aurora PostgreSQL.
  4. **Mainframe:** entender COBOL, extraer reglas de negocio y reimaginarlas como cloud-native.
  5. **AWS Transform custom:** actualizaciones de lenguaje y framework (Java, Python, Node, SDKs, Graviton) y tus
     propias transformaciones.
- Anillo exterior: **las personas aprueban cada paso**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Núcleo "AWS Transform" | `entrar` | `scale-in` | El halo con `pulse` en `loop` |
| Tarjetas 1–5 | `clic 1..5` | `scale-in` desde el núcleo hasta su posición en la órbita (`morph`) | 600ms cada una |
| Órbita (círculo punteado) | `clic 1` | `draw-line` | 1000ms |
| Anillo "las personas aprueban" | `clic 6` | `draw-line` alrededor de todo | Termina con un `stamp` del icono de persona |

**Sin animación:** el núcleo y la órbita visibles; las tarjetas aparecen por clic en su posición final.

**Notas del orador:**
> AWS Transform es la respuesta de AWS a esa deuda: agentes especializados en migración y modernización.
> *(clics)* Assessment, migración de VMware, Windows y .NET, mainframe, y Transform custom para actualizaciones y
> transformaciones propias. *(clic)* Y algo clave: las personas aprueban cada paso. Los agentes hacen el trabajo; las
> decisiones son humanas.
>
> *Antes de cada charla, verificar esta lista en las páginas oficiales de AWS Transform.*

**Transición:** fundido.

---

### Diapositiva 6. Cómo trabaja: el loop con humano

**Objetivo:** mostrar el mecanismo en un solo dibujo.

**En pantalla**
- Un ciclo con cinco nodos: **Descubrir → Evaluar → Planear → Transformar → Validar**
- Entre cada par de nodos, una compuerta: **✓ Aprobación humana**
- Frase inferior: **"El agente propone. Tú decides."**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Ciclo | `entrar` | `draw-line` | 1200ms |
| Nodos | `entrar` | `stagger` de `scale-in` | 120ms entre nodos |
| Partícula "trabajo" | `loop` | `flow` por el ciclo | 4s por vuelta; se detiene 0.6s en cada compuerta, que hace un `pulse` |
| Compuertas | `clic 1` | `stagger` de `stamp` | 100ms entre compuertas |
| Frase inferior | `clic 2` | `split-words` | — |

**Sin animación:** el ciclo completo con las compuertas; la frase aparece por clic.

**Notas del orador:**
> Por dentro, una migración es un ciclo: descubrir, evaluar, planear, transformar y validar. *(clic)* Entre cada paso hay
> una compuerta, donde un humano aprueba o pide cambios. *(clic)* El agente propone; tú decides.

**Transición:** zoom hacia el centro del ciclo para la diapositiva 7.

---

### Diapositiva 7. ¿Por qué un mini-demo?

**Objetivo:** justificar la réplica y ser honestos.

**En pantalla**
- Izquierda: una arquitectura grande y densa, con muchos servicios conectados
- Texto: **Un demo completo de AWS Transform se integra con muchos servicios y toma horas.**
- Derecha, tras el clic: una caja pequeña y nítida: **Mini-demo · lo construimos con Amazon Bedrock AgentCore**
- Nota honesta abajo: *No es AWS Transform. Es una réplica transparente del loop que corren sus agentes.*

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Arquitectura grande | `entrar` | `stagger` de `fade-up` (los nodos) y `draw-line` (las conexiones) | Rápido: 40ms entre nodos |
| Texto | `entrar` | `fade-up` | +800ms |
| La arquitectura se desenfoca y retrocede | `clic 1` | `blur-in` invertido (0→8px) + escala 1→0.9 | 700ms |
| Caja "Mini-demo" | `clic 1` | `morph` desde el centro de la arquitectura hasta la derecha | 800ms |
| Nota honesta | `clic 2` | `fade-up` | — |

**Sin animación:** la arquitectura desenfocada a la izquierda y la caja a la derecha. La nota aparece por clic.

**Notas del orador:**
> AWS Transform es un servicio grande, integrado con otros servicios de AWS; un demo completo nos tomaría la tarde.
> *(clic)* Así que construimos un mini-demo que replica sus características con Amazon Bedrock AgentCore.
> *(clic)* Y quiero ser muy claro: esto no es AWS Transform. Es una réplica transparente, para que veamos qué pasa por
> dentro. Cuando entiendes el mecanismo, entiendes el producto.

**Transición:** fundido.

---

### Diapositiva 8. Una transformación, tres stacks

**Objetivo:** presentar el título de la charla como un mapa.

**En pantalla.** Tres columnas, cada una armada como una pila de capas:

| Legacy | Agentes | Target |
|---|---|---|
| EC2 t3.large | Amazon Bedrock AgentCore Runtime | Kubernetes |
| Flask (monolito) | Strands Agents | Terraform (VPC, EKS, ECR, RDS) |
| SQLite en disco local | Amazon Nova 2 Lite · DeepSeek V3.2 | Kustomize (manifiestos) |
| cron + nginx | 6 agentes + compuertas humanas | Contenedor no-root + pruebas |

Arriba: **Una transformación** · abajo: **tres stacks**.

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Columna Legacy | `clic 1` | `stack-build` | Al final, una capa con `shake-soft` (la deuda) |
| Columna Agentes | `clic 2` | `stack-build` | Al final, `pulse` en "6 agentes" |
| Columna Target | `clic 3` | `stack-build` | Al final, `stamp` "✓" |
| Flechas Legacy→Agentes→Target | `clic 3` | `draw-line` + `flow` en `loop` | — |
| Textos "Una transformación" y "tres stacks" | `entrar` | `fade-up` | — |

**Sin animación:** las columnas completas aparecen por clic, sin movimiento; las flechas quedan estáticas.

**Notas del orador:**
> Este es el mapa de la charla. *(clic)* Venimos de aquí: un monolito Flask con SQLite en una EC2 que se hizo a mano.
> *(clic)* En el medio, el stack que hace el trabajo: seis agentes en AgentCore, con Strands y modelos de Bedrock.
> *(clic)* Y llegamos aquí: un microservicio en Kubernetes, con Terraform para la infraestructura y Kustomize para los
> manifiestos. Una transformación, tres stacks. Veamos a los agentes uno por uno.

**Transición:** la columna "Agentes" hace un `morph` y se convierte en la fila de 6 agentes de la diapositiva 9.

---

### Diapositivas 9–14. Los seis agentes

**Plantilla común para las seis.** Mantener la misma estructura, para que el público aprenda a leerla:

- **Arriba:** una fila con los 6 agentes y el actual resaltado (progreso). Se anima con un `morph` del resaltado del
  agente anterior al actual (400ms).
- **Izquierda:** la ilustración animada única de cada agente (el corazón visual).
- **Derecha:** cuatro bloques cortos:
  - **Qué hace**
  - **Herramientas (deterministas)**
  - **El modelo decide**
  - **En AWS Transform**
- **Abajo:** la cifra clave del demo, con `count-up`.

**Animación común de los bloques de la derecha:** `stagger` de `fade-up` en `clic 1`. La cifra clave usa `count-up` en
`clic 2`.

---

### Diapositiva 9. Agente 1: Descubrimiento y evaluación

**En pantalla**
- **Qué hace:** lee el código y la infraestructura, y encuentra los problemas.
- **Herramientas:** escaneo de código (rutas, secretos, SQL concatenado, estado local), lectura del Terraform legacy,
  EC2 y CloudWatch en vivo.
- **El modelo decide:** la severidad, la estrategia (6R) y el puntaje de preparación.
- **En AWS Transform:** descubrimiento y evaluación a escala de portafolio.
- **Cifra:** **17 hallazgos · 3 críticos** · preparación **50/100** · estrategia **replatform**

**Ilustración animada:** un radar sobre una cuadrícula de archivos de código (`app.py`, `schema.sql`,
`legacy-inventory.service`, `main.tf`).

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Cuadrícula de archivos | `entrar` | `stagger` de `fade-up` | 60ms entre archivos |
| Barrido de radar | `loop` | `radar` | 2.4s |
| Pines de hallazgo sobre los archivos | `entrar` | `stagger` de `stamp` | +800ms, 90ms entre pines; 3 pines rojos con `pulse` |
| "17" y "3 críticos" | `clic 2` | `count-up` | El "3" en rojo con `pulse` |

**Notas del orador:**
> El primer agente hace arqueología, pero sin adivinar. *(clic)* Las herramientas deterministas encuentran la
> evidencia: rutas, secretos en el código, una búsqueda con SQL concatenado, reportes en el disco local. El modelo solo
> pondera y decide la estrategia. *(clic)* Diecisiete hallazgos, tres críticos, en unos diez segundos. Por eso es rápido,
> y por eso cada hallazgo se puede auditar.

---

### Diapositiva 10. Agente 2: Requisitos

**En pantalla**
- **Qué hace:** convierte el documento del negocio en una especificación validada y trazable.
- **Herramientas:** lectura del documento, plantilla de especificación, validador de la especificación.
- **El modelo decide:** prioridades, criterios de aceptación y supuestos para las preguntas abiertas.
- **En AWS Transform:** definición del objetivo con aclaraciones humanas en el loop.
- **Cifra:** **24 requisitos** · presupuesto **$450/mes** · **3 preguntas abiertas → 2 resueltas por ti**

**Ilustración animada:** una hoja del documento (`migration-requirements.md`). Un resaltador barre líneas que "vuelan"
hacia tarjetas ordenadas de la especificación.

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Documento | `entrar` | `fade-up` | — |
| Líneas resaltadas | `entrar` | `stagger` de `highlight-sweep` | 150ms entre líneas |
| Líneas → tarjetas de la especificación | `entrar` | `morph` de cada línea a su tarjeta | +900ms, 100ms entre tarjetas |
| Tres burbujas "?" (preguntas abiertas) | `clic 2` | `stagger` de `scale-in` | — |
| Dos burbujas se convierten en "✓ Resuelta" | `clic 3` | `morph` + `stamp` | El "?" restante queda con `pulse` |

**Notas del orador:**
> El segundo agente lee lo que pidió el negocio: un presupuesto de 450 dólares al mes, un SLO de 99.9 y Graviton.
> *(clic)* Cada requisito queda con prioridad y criterio de aceptación. *(clic)* Y lo importante: no inventa respuestas.
> Deja tres preguntas abiertas con sus supuestos. *(clic)* En el demo le respondo en lenguaje natural, y resuelve dos.
> La tercera, la fecha del cutover, sigue abierta, porque no la respondí. Eso es el humano en el loop.

---

### Diapositiva 11. Agente 3: Modernización del código

**En pantalla**
- **Qué hace:** convierte el monolito en un microservicio contenerizado.
- **Herramientas:** un **patrón probado** (config por variables de entorno, health y readiness, logs JSON, Dockerfile
  no-root de solo lectura, manifiestos Kustomize), las pruebas del servicio y la validación de manifiestos.
- **El modelo decide:** porta la lógica de negocio de las rutas y escribe las pruebas.
- **En AWS Transform:** transformación de código y contenerización (.NET, Java, mainframe, custom).
- **Cifra:** **23/23 pruebas** · **32 archivos** · inyección SQL **corregida**

**Ilustración animada:** el bloque del monolito se fractura y sus piezas se reordenan en un contenedor.

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Bloque "monolito" (con grietas) | `entrar` | `scale-in` | — |
| Fractura y reconstrucción en un cubo "contenedor" | `entrar` | `crack-rebuild` | +500ms, 1400ms |
| Capa inferior del cubo: "patrón probado (herramienta)" | `entrar` | `stack-build` | +1900ms |
| Capa superior: "lógica de negocio (modelo)" | `entrar` | `stack-build` | +2100ms, con un `pulse` del color del modelo |
| "23/23" | `clic 2` | `count-up` | Al terminar, `stamp` "✓" |

**Notas del orador:**
> Aquí aplica la idea más importante de la charla. El agente no escribe un microservicio desde cero. *(clic)* Las
> herramientas montan un patrón probado, y el modelo solo porta lo que requiere criterio: la lógica de negocio. Después
> corre las pruebas y, si fallan, las arregla. *(clic)* Veintitrés de veintitrés. Mismo contrato de API, sin inyección
> SQL y con la sobreventa protegida.

---

### Diapositiva 12. Agente 4: Infraestructura como código (Terraform)

**En pantalla**
- **Qué hace:** escribe la infraestructura target: VPC, EKS, ECR y RDS PostgreSQL.
- **Herramientas:** un **blueprint validado**, `terraform validate` y verificaciones contra la especificación.
- **El modelo decide:** cómo mapear la especificación a las variables del blueprint.
- **En AWS Transform:** conversión de red e infraestructura a IaC (CloudFormation, CDK o Terraform).
- **Cifra:** **terraform validate ✓** · **17/17** verificaciones · Kubernetes **1.33** *(recuérdalo)*

**Ilustración animada:** a la izquierda, "desde cero": código que se escribe y se tacha. A la derecha, "con blueprint":
una cuadrícula azul que se dibuja y se llena.

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Panel "Desde cero" | `entrar` | `typewriter` de unas líneas HCL | — |
| Sello "✗ inválido" en "Desde cero" | `entrar` | `stamp` + `shake-soft` | +1200ms |
| Panel "Con blueprint" (cuadrícula) | `clic 1` | `draw-line` | 900ms |
| Las variables entran a su casilla | `clic 1` | `stagger` de `morph` | 80ms entre variables |
| Sello "✓ terraform validate" | `clic 2` | `stamp` | — |
| "1.33" | `clic 2` | `highlight-sweep` | Queda con un `pulse` muy suave (es la semilla del "aha") |

**Notas del orador:**
> Primero dejamos que los modelos escribieran esta infraestructura desde cero. Todos fallaron: usaron argumentos
> antiguos del módulo de EKS, con mucha seguridad y desactualizados. *(clic)* Con un blueprint validado pasaron al primer
> intento. *(clic)* Grounding le gana a la generación. Y fíjense en un detalle: Kubernetes 1.33. Guárdenlo, que va a
> volver.

---

### Diapositiva 13. Agente 5: Validación de Kubernetes

**En pantalla**
- **Qué hace:** valida los manifiestos contra el esquema y las políticas, los corrige y los despliega de verdad.
- **Herramientas:** kubeconform, 16 políticas (límites, probes, no-root, sin `:latest`, PDB…) y un despliegue en un
  clúster kind con pruebas de humo.
- **El modelo decide:** cómo corregir cada violación.
- **En AWS Transform:** validación y pruebas antes del cutover.
- **Cifra:** **4 violaciones → 4 corregidas → 0** · pruebas de humo **200 · 200 · 200 · 201 · 409**

**Ilustración animada:** un fragmento de YAML con cuatro líneas en rojo que se corrigen en verde. Después, unos pods
(puntos) arrancan en un anillo de clúster.

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| YAML | `entrar` | `fade-up` | — |
| Cuatro líneas en rojo (`:latest`, sin límites, filesystem escribible, sin PDB) | `entrar` | `stagger` de `highlight-sweep` rojo + `shake-soft` | 120ms entre líneas |
| Las líneas pasan a verde | `clic 1` | `stagger` de `highlight-sweep` verde | 120ms entre líneas |
| Anillo del clúster y pods | `clic 2` | `draw-line` del anillo + `stagger` de `scale-in` de los pods | Los pods con `pulse` lento |
| Pruebas de humo 200·200·200·201·409 | `clic 2` | `stagger` de `stamp` | El 409 con la etiqueta "sobreventa bloqueada ✓" |

**Notas del orador:**
> Ahora, la hora de la verdad: ¿corre? Simulamos algo que pasa en todo proyecto: alguien edita los manifiestos a mano.
> *(clic)* El agente detecta las cuatro violaciones y las corrige. *(clic)* Luego construye la imagen, la despliega en un
> Kubernetes real y corre pruebas de humo. Doscientos, doscientos, doscientos, la orden se crea con un 201, y el intento
> de sobreventa devuelve 409: la regla de negocio sobrevivió a la migración.

---

### Diapositiva 14. Agente 6: FinOps

**En pantalla**
- **Qué hace:** calcula el costo actual, el del diseño y el optimizado, y verifica el presupuesto.
- **Herramientas:** el motor de costos en Python, los precios de AWS (snapshot fechado), el ajuste de pods y nodos.
- **El modelo decide:** qué optimizaciones aplicar y cómo explicar los trade-offs.
- **En AWS Transform:** assessment y caso de negocio.
- **Cifra:** **$72 hoy → $623 diseñado → $208 optimizado** · presupuesto **$450**

**Ilustración animada:** gráfica de tres barras con una línea de presupuesto. Este es el "aha" de la charla.

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Línea de presupuesto $450 (punteada) | `entrar` | `draw-line` | — |
| Barra "Hoy $72" | `entrar` | `grow-bar` + `count-up` | — |
| Barra "Diseñado" (base) | `clic 1` | `grow-bar` | Sube hasta $258 |
| Segmento rojo "+$365 soporte extendido" | `clic 2` | Cae desde arriba y se apila (`stack-build`) + `shake-soft` de la barra | Al terminar, el total `count-up` hasta $623, por encima del presupuesto |
| Callout "Kubernetes 1.33 está en soporte extendido de EKS" | `clic 2` | `typewriter` | Une visualmente con el "1.33" de la diapositiva 12 |
| Barra "Optimizado $208" | `clic 3` | `grow-bar` + `count-up` | Al terminar, `stamp` "✓ dentro del presupuesto" |

**Sin animación:** las barras en su altura final, con los clics revelando cada estado.

**Notas del orador:**
> Y la pregunta de todo CFO: ¿cuánto cuesta? Hoy, unos 72 dólares al mes. *(clic)* El diseño… *(clic)* 623, por encima del
> presupuesto. ¿Por qué? ¿Recuerdan el 1.33? Está en soporte extendido de EKS: 365 dólares al mes que ni la
> especificación ni el agente de IaC vieron. Lo atrapó el agente de FinOps. *(clic)* Optimizado: 208, dentro del
> presupuesto. Y seamos honestos: sigue costando unas tres veces más que un servidor. Lo que se paga es alta
> disponibilidad, una base de datos Multi-AZ, autoescalado y los hallazgos de seguridad corregidos. Cada número sale de
> código probado, no del modelo.

**Transición:** las seis tarjetas de agentes se agrupan (`morph`) y forman la cuadrícula de lecciones de la
diapositiva 15.

---

### Diapositiva 15. Cinco lecciones

**Objetivo:** dejar las ideas que importan más que cualquier lista de features.

**En pantalla.** Cinco tarjetas, cada una con un icono de trazo:
1. **Grounding le gana a la generación.** Conocimiento curado y validado, y el modelo encima.
2. **Las herramientas cuentan; el modelo decide.** La evidencia es auditable.
3. **El humano en el loop es una feature.** Migrar es decidir riesgo y costo.
4. **La validación no es negociable.** Generar sin validar es deuda técnica rápida.
5. **El costo es parte del diseño.** No una hoja de cálculo tres meses después.

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Tarjeta 1 | `clic 1` | `flip-in` + `draw-line` del icono | — |
| Tarjeta 2 | `clic 2` | `flip-in` + `draw-line` del icono | — |
| Tarjeta 3 | `clic 3` | `flip-in` + `draw-line` del icono | — |
| Tarjeta 4 | `clic 4` | `flip-in` + `draw-line` del icono | — |
| Tarjeta 5 | `clic 5` | `flip-in` + `draw-line` del icono | — |
| Tarjeta activa | `loop` | `pulse` | Solo la última que entró |

**Sin animación:** las tarjetas aparecen por clic, de frente.

**Notas del orador:**
> Construimos esto para entenderlo, y estas son las cinco cosas que aprendimos. *(clic)* Los modelos son brillantes
> razonando y poco confiables recordando versiones; anclados a conocimiento curado son rápidos y correctos. *(clic)* En
> el demo el modelo nunca suma un número. *(clic)* Cada agente se detuvo a esperarme, porque migrar es una decisión de
> negocio. *(clic)* Los momentos más valiosos fueron las validaciones. *(clic)* Y el agente de FinOps encontró un error de
> 365 dólares al mes que nadie le pidió buscar.

**Transición:** fundido.

---

### Diapositiva 16. ¿Por qué AWS Transform importa?

**Objetivo:** cerrar el argumento: no hace falta construir esto; hay que entenderlo y usarlo.

**En pantalla**
- Título: **Construye lo que te hace único. Usa lo que ya existe.**
- Izquierda, **Nuestro mini-demo:** 1 aplicación, 6 agentes, blueprints curados a mano.
- Derecha, **AWS Transform:** cientos de aplicaciones; VMware, .NET, mainframe y actualizaciones; conocimiento curado
  y mantenido; agentes, validación y aprobación humana integrados.
- Franja inferior: **AWS Transform para la fábrica de migración · AgentCore para lo que te diferencia**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Título | `entrar` | `split-words` | — |
| Panel izquierdo | `clic 1` | `fade-up` | — |
| Panel derecho | `clic 2` | `scale-in` más grande + `stagger` de sus puntos | Contraste de escala: lo pequeño frente a lo grande |
| Franja inferior | `clic 3` | `highlight-sweep` de lado a lado | — |

**Sin animación:** los paneles aparecen por clic.

**Notas del orador:**
> Quizás estén pensando: si lo puedo construir con AgentCore, ¿para qué AWS Transform? *(clic)* Porque nosotros hicimos
> un demo del mecanismo para una sola aplicación, y fue mucho trabajo curar ese conocimiento. *(clic)* Su realidad son
> cientos de aplicaciones, mainframes, VMware y .NET. AWS Transform trae ese loop listo, gestionado y a escala.
> *(clic)* Usen AWS Transform para la fábrica de migración, y AgentCore para lo que los hace diferentes. Y ahora que ya
> vieron por dentro, saben qué esperar y qué verificar.

**Transición:** fundido a oscuro para la diapositiva 17.

---

### Diapositiva 17. Demo en vivo (transición)

**Objetivo:** pasar al demo con energía.

**En pantalla**
- Fondo oscuro
- Una terminal centrada: `$ make ui` → `Hi, let's migrate your legacy app.`
- Texto: **Demo en vivo · 15 minutos · Legacy → Agentes → Target**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Terminal | `entrar` | `scale-in` | — |
| `$ make ui` | `entrar` | `typewriter` | — |
| Respuesta "Hi, let's migrate your legacy app." | `entrar` | `typewriter` | +600ms |
| Texto inferior | `entrar` | `fade-up` | +1400ms |
| Cursor de la terminal | `loop` | Parpadeo | 1s |

**Sin animación:** la terminal con el texto completo.

**Notas del orador:**
> Basta de diapositivas. Vamos a migrar ese servidor, en vivo, con los seis agentes. *(cambiar a la app: modo
> presentador, `?presenter=1`)*
>
> *Guion del demo: [docs/KEYNOTE-SCRIPT.md](KEYNOTE-SCRIPT.md), Parte 2. Plan B: menú "…" → "Replay a Recorded Run
> (Offline)".*

---

### Diapositiva 18. Gracias + 3 QR

**Objetivo:** cerrar con comunidad y dejar los enlaces durante el Q&A.

**En pantalla**
- **¡Gracias!** · Santiago Castellanos
- Tres tarjetas, cada una con un código QR y su etiqueta:
  1. **Comunidad CloudCrafters:** `[URL_CLOUDCRAFTERS]`
  2. **AI AWS UG Colombia:** `[URL_AI_AWS_UG_COLOMBIA]`
  3. **Repositorio de workshops:** `[URL_REPOSITORIO]`
- Línea inferior: **¿Preguntas?**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| "¡Gracias!" | `entrar` | `split-words` | — |
| Tarjetas QR | `entrar` | `stagger` de `flip-in` | +600ms, 180ms entre tarjetas |
| Marco de cada QR | `entrar` | `draw-line` alrededor | Al terminar cada `flip-in` |
| "¿Preguntas?" | `entrar` | `fade-up` | +1500ms |
| Fondo | `loop` | `drift` | Muy suave |

**Sin animación:** las tres tarjetas visibles.

**Reglas para los QR:**
- Tamaño mínimo de 280px en pantalla, con margen blanco ("quiet zone").
- Contraste negro sobre blanco, aunque la diapositiva sea oscura.
- Probarlos con un celular desde el fondo de la sala.
- El repositorio debe ser público.

**Notas del orador:**
> Gracias. Todo lo que vieron está en el repositorio: los agentes, la UI, el guion y las especificaciones para
> mantenerlo. Únanse a CloudCrafters y a AI AWS UG Colombia: así es como seguimos aprendiendo juntos. ¿Preguntas?

---

## 3. Lista de verificación antes de la charla

- [ ] `make numbers` coincide con las cifras de las diapositivas 9–14. Si se re-grabó el demo, actualizar.
- [ ] La lista de capacidades de AWS Transform (diapositiva 5) está verificada en las páginas oficiales.
- [ ] Las URLs de los QR están reemplazadas, probadas con un celular, y el repositorio es público.
- [ ] El botón "Pausar animaciones" y la tecla P funcionan; la charla completa se puede dar en pausa.
- [ ] Probado en el proyector a 1280×720: trazos visibles y texto legible desde el fondo.
- [ ] El demo está preparado según [WORKSHOP.md](../WORKSHOP.md) §6: `make preflight`, kind, la app en modo presentador y
      el plan B listo.
