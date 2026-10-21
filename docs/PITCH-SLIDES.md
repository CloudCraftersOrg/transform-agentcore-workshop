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
| 3 | El número que nadie presupuesta | 2:00 |
| 4 | Todo lo que pasa por la cabeza | 2:30 |
| 5 | AWS Transform: acelerar la modernización | 2:30 |
| 6 | ¿Qué incluye AWS Transform? | 2:30 |
| 7 | ¿Por qué un mini-demo? | 1:30 |
| 8 | Una transformación, tres stacks | 2:00 |
| 9–14 | Los seis agentes (1:30 c/u) | 9:00 |
| 15 | Cinco lecciones | 2:30 |
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

### Diapositiva 3. El número que nadie presupuesta

**Objetivo:** abrir con un dato contundente, con su fuente, y con números que se mueven.

**En pantalla**
- Antetítulo pequeño: **Antes de empezar, un número.**
- Cifra protagonista: **US$ 100 mil millones**
- Línea bajo la cifra: **desperdiciados en sobrecostos de migración a la nube en solo tres años**, y **+14 % de gasto
  sobre lo planeado, cada año**.
- Tres tarjetas de datos (por clic):
  1. **38 %** de las empresas ha visto su migración retrasada más de un trimestre. *(McKinsey, 2021)*
  2. **61 %** de los líderes reporta una "fatiga de migración" que retrasa proyectos seis meses o más. *(CloudBees,
     2025)*
  3. **84 %** dice que gestionar el gasto en la nube es su mayor reto. *(Flexera, 2025)*
- Remate: **El problema no es llegar a la nube. Es el camino.**
- Pie de fuentes, pequeño pero legible:
  - McKinsey, *Cloud-migration opportunity: Business value grows, but missteps abound* (2021)
  - CloudBees, *DevOps Migration Index* (nov. 2025)
  - Flexera, *2025 State of the Cloud Report* (mar. 2025)

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Antetítulo | `entrar` | `fade-up` | 400ms |
| "US$ 100" | `entrar` | `count-up` | +300ms, 0→100 en 1800ms, dígitos tabulares, con un leve desenfoque de movimiento mientras corre |
| "mil millones" | `entrar` | `blur-in` | Al terminar el conteo |
| Línea bajo la cifra | `entrar` | `split-words` | +2300ms; "+14 %" hace su propio `count-up` (0→14) |
| Fuente McKinsey bajo la cifra | `entrar` | `fade-up` | +2900ms |
| Tarjeta 38 % | `clic 1` | `flip-in` + `count-up` 0→38 | La barra de progreso interna crece hasta el 38 % (`grow-bar` horizontal) |
| Tarjeta 61 % | `clic 2` | `flip-in` + `count-up` 0→61 | Igual |
| Tarjeta 84 % | `clic 3` | `flip-in` + `count-up` 0→84 | Igual; la barra termina con un `pulse` |
| Remate | `clic 4` | `split-words` | "el camino" con `highlight-sweep` |
| Pie de fuentes | `clic 4` | `fade-up` | — |

**Sin animación:** la cifra y su línea visibles en su valor final; las tarjetas y el remate aparecen por clic, sin
movimiento.

**Notas del orador:**
> Antes de empezar, un número. *(deja correr el contador)* Cien mil millones de dólares. Según McKinsey, eso es lo que
> se desperdició en sobrecostos de migración a la nube en solo tres años. En promedio, un 14 % más de gasto del
> planeado, cada año. *(clic)* Más de un tercio de las empresas vio su migración retrasada más de un trimestre.
> *(clic)* Y no es cosa del pasado: en 2025, seis de cada diez líderes hablan de "fatiga de migración", proyectos
> frenados seis meses o más. *(clic)* Y el 84 % dice que su mayor reto es controlar lo que gasta en la nube.
> *(clic)* El problema no es llegar a la nube. Es el camino.

**Transición:** la cifra se encoge y se desvanece hacia el centro, donde aparece la persona de la diapositiva 4
(`morph`, 600ms).

**Verificación de los datos (antes de cada charla):**

| Dato | Fuente | Nota |
|---|---|---|
| ~US$100 mil millones en 3 años; +14 %/año; 38 % retrasadas más de un trimestre | [McKinsey (2021)](https://www.mckinsey.com/industries/technology-media-and-telecommunications/our-insights/cloud-migration-opportunity-business-value-grows-but-missteps-abound) | Encuesta a ~450 CIOs y responsables de TI (2020–21). Es una proyección; decir el año en voz alta. |
| 61 % fatiga de migración (retrasos de 6+ meses); 18 % de sobrecosto promedio | [CloudBees (10 nov. 2025)](https://www.cloudbees.com/newsroom/cloudbees-first-devops-migration-index) | 300+ líderes de TI; migraciones de plataformas DevOps, no solo de nube. |
| 84 %: gestionar el gasto en la nube es el mayor reto | [Flexera (19 mar. 2025)](https://www.flexera.com/about-us/press-center/new-flexera-report-finds-84-percent-of-organizations-struggle-to-manage-cloud-spend) | 750+ encuestados. |

No usar cifras que circulan sin fuente primaria (por ejemplo, "83 % de las migraciones fracasan").

---

### Diapositiva 4. Todo lo que pasa por la cabeza

**Objetivo:** que el público sienta la carga mental de una migración. Cada clic suma un problema a la cabeza de una
persona, hasta que todo se resume en una palabra: **conocimiento**.

**En pantalla**
- En el centro, una persona dibujada con trazo lineal: de medio cuerpo, con la mano en el mentón, pensando.
- Burbujas de pensamiento que se acumulan alrededor y encima de la cabeza (por clic):
  - **Al entrar:** "Ese servidor que nadie quiere tocar" (retoma el gancho clásico)
  - **Clic 1 (el código):** "¿Qué hace realmente esta app?" · "Quien la escribió se fue en 2019" · "Cero documentación"
  - **Clic 2 (datos y dependencias):** "¿De qué depende?" · "SQLite en el disco local" · "¿Dónde están los secretos?"
  - **Clic 3 (el riesgo):** "No hay staging" · "¿Qué se rompe si la muevo?" · "¿Cuándo es la ventana de cutover?"
  - **Clic 4 (el negocio):** "Presupuesto: US$450/mes" · "SLO 99,9 %" · "El equipo ya está saturado" · "Fecha límite:
    Q1"
- A un lado, un medidor vertical: **carga mental**, que sube con cada clic.
- **Clic 5:** las burbujas se comprimen dentro de la cabeza y salen convertidas en una sola palabra: **Conocimiento**.
  Debajo aparece la tesis: **Las migraciones no fallan por el código. Fallan por el conocimiento.**

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Persona (trazo) | `entrar` | `draw-line` | 1400ms, trazo de 3px |
| Respiración leve de la persona | `loop` | `pulse` (escala 1→1.01) | 4s |
| Burbuja inicial | `entrar` | `scale-in` desde la cabeza | +1200ms, con 3 puntitos que suben antes (los "pensamientos") |
| Burbujas del clic N | `clic 1..4` | `stagger` de `scale-in` desde la cabeza hasta su posición (`morph`) | 120ms entre burbujas. Las burbujas anteriores se empujan suavemente para hacer espacio (500ms, `firme`) |
| Color de las burbujas | `clic 1..4` | Transición de color | De neutro (clic 1) a ámbar (clic 3) y a rojo suave (clic 4) |
| Cabeza | `clic 1..4` | Inclinación | 2° más hacia abajo por clic (el peso de los problemas); en el clic 4, además, una gota de sudor con `fade-up` |
| Medidor de carga mental | `clic 1..4` | `grow-bar` vertical | 25 % por clic; en el clic 4 llega al tope y hace `shake-soft` |
| Compresión de burbujas | `clic 5` | `morph` de todas las burbujas hacia la cabeza | 700ms, `firme` |
| Palabra "Conocimiento" | `clic 5` | `scale-in` desde la cabeza + `highlight-sweep` | +700ms |
| Tesis | `clic 5` | `split-words` | +1100ms |
| Cabeza y medidor | `clic 5` | Vuelven a su sitio (alivio) | 600ms, `suave` |

**Sin animación:** la persona dibujada, con las burbujas de cada clic en su lugar y su color, y el medidor en su nivel.
En el clic 5 se ven solo la palabra y la tesis.

**Reglas de legibilidad:**
- Como máximo 14 burbujas en total.
- Texto de las burbujas de 24px o más; en un proyector, mejor menos burbujas que texto pequeño.
- Las burbujas no deben tapar la cara.

**Notas del orador:**
> Ahora pónganse en los zapatos de quien recibe esta migración. *(entra la burbuja)* Empieza con ese servidor que nadie
> quiere tocar. *(clic)* ¿Qué hace realmente? Quien la escribió se fue en 2019, y no hay documentación. *(clic)* ¿De qué
> depende? Los datos viven en un SQLite en el disco, y los secretos… en el código. *(clic)* No hay staging. ¿Qué se rompe
> si la muevo? ¿Cuándo hacemos el cutover? *(clic)* Y el negocio: 450 dólares al mes, 99,9 % de disponibilidad, un
> equipo que ya está saturado y una fecha límite. *(pausa)*
> *(clic)* Todo esto se resume en una palabra: conocimiento. Las migraciones no fallan por el código. Fallan por el
> conocimiento. Y eso es justo lo que AWS Transform viene a resolver.

**Transición:** la palabra "Conocimiento" hace un `morph` hacia el núcleo "AWS Transform" de la diapositiva 5.

---

### Diapositiva 5. AWS Transform: acelerar la modernización

**Objetivo:** presentar AWS Transform con fuerza: el logo en el centro y un solo diagrama que cuenta el viaje
completo. Los sistemas legados entran, recorren cuatro etapas alrededor del servicio y salen convertidos en una
arquitectura cloud-native, con un humano al mando.

**Referencia visual:** el diagrama "Accelerating Modernization with AWS Transform" (un anillo de cuatro etapas
alrededor de un núcleo, y una flecha de Legacy a Cloud-Native en la base). Se replica el layout, con dos cambios:
- **Las etapas siguen el orden lógico**, en sentido horario desde la entrada de Legacy: Descubrimiento → Planificación
  → Refactorización → Supervisión humana → salida hacia Cloud-Native.
- **El icono "GDPR" de la referencia se reemplaza** por una mano con un check: supervisión humana, sin aludir a una
  normativa concreta.

**Logo de AWS Transform:**
- **Fuente:** el icono oficial del paquete *AWS Architecture Icons*
  ([aws.amazon.com/architecture/icons](https://aws.amazon.com/architecture/icons/)), en SVG. Buscar "AWS Transform"
  dentro del ZIP.
- **Uso:** sin recolorear ni deformar, con su margen de respeto, y siguiendo los términos de uso de AWS. No recrearlo a
  mano.

**Layout** (lienzo de 1920×1080; ajustar a la plantilla manteniendo las proporciones)

| Zona | Contenido | Posición aproximada |
|---|---|---|
| Título | **Acelerando la modernización con AWS Transform** | Centrado arriba, y≈90 |
| Núcleo | El logo oficial de AWS Transform (≈120px), el nombre **AWS Transform** y el subtítulo *Agentes de IA para migrar y modernizar* | Círculo interior, centro (960, 500), radio ≈150 |
| Anillo | Cuatro arcos (cuadrantes) de trazo grueso (6px), separados por cortes radiales, abierto en la base. La entrada viene de la caja Legacy (abajo a la izquierda); la salida baja hacia la flecha (abajo a la derecha) | Radio exterior ≈300, alrededor del núcleo |
| Etapa 1 (izquierda) | Icono de globo con lupa · **Descubrimiento automatizado** · *Identifica aplicaciones y dependencias* | Cuadrante izquierdo-inferior; la etiqueta afuera, a la izquierda |
| Etapa 2 (arriba a la izquierda) | Icono de brújula o ruta · **Planificación de la migración** · *Genera oleadas y mapeos de migración* | Cuadrante superior izquierdo; la etiqueta afuera, arriba a la izquierda |
| Etapa 3 (arriba a la derecha) | Icono de laptop con engranaje · **Refactorización con IA** · *Convierte código legado en moderno* | Cuadrante superior derecho; la etiqueta afuera, arriba a la derecha |
| Etapa 4 (derecha) | Icono de mano con check · **Supervisión humana** · *Revisa y valida los cambios críticos* | Cuadrante derecho-inferior; la etiqueta afuera, a la derecha |
| Base, izquierda | Caja con borde rojo e icono de alerta · **Sistemas legados** · *Modernización lenta, riesgosa y costosa* | Caja de x≈420 a x≈900, y≈850; el texto debajo |
| Base, derecha | Cuerpo de flecha que sale de la caja hacia la derecha, con punta grande. Icono de nube con servidor sobre la punta · **Arquitectura cloud-native** · *Negocio rápido, ágil e innovador* | De x≈900 a x≈1500, y≈850; el texto debajo de la punta |

**Colores** (sugeridos y seguros en proyector; la plantilla puede usar su paleta manteniendo cuatro tonos distintos):
- **Descubrimiento:** dorado oscuro `#C99A06`
- **Planificación:** verde azulado `#0E9F84`
- **Refactorización:** naranja `#E07020`
- **Supervisión humana:** azul `#2F7FC1`
- **Legacy:** borde rojo `#C4314B`
- **Cloud-native:** flecha en degradado de azul a índigo (`#2F7FC1` → `#5B5FC7`)
- **Textos:** título de cada etapa en el color de su arco; la descripción en gris oscuro.

**Iconos:** de línea, con trazo uniforme de 2–3px y sin relleno. El logo de AWS Transform es la única pieza a todo
color.

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Fondo | `loop` | `drift` | Muy suave |
| Título | `entrar` | `split-words` | — |
| Logo de AWS Transform | `entrar` | `scale-in` | +300ms, 700ms, `rebote` (la única vez en la diapositiva); luego un halo con `pulse` en `loop` |
| Nombre y subtítulo del núcleo | `entrar` | `fade-up` | +800ms |
| Círculo interior | `entrar` | `draw-line` | +300ms, 1000ms |
| Caja Legacy | `entrar` | `draw-line` del borde rojo + `fade-up` del texto | +1300ms; al final, un `shake-soft` (el dolor) |
| Etapa 1: Descubrimiento | `clic 1` | `draw-line` del arco desde la caja Legacy hacia arriba (sentido horario) + `stamp` del icono + `fade-up` de la etiqueta | 700ms; arranca una partícula `flow` desde Legacy por el arco ya dibujado |
| Etapa 2: Planificación | `clic 2` | Igual, siguiente arco | La partícula sigue el recorrido |
| Etapa 3: Refactorización | `clic 3` | Igual | — |
| Etapa 4: Supervisión humana | `clic 4` | Igual; además, junto al icono, aparece la frase **"El agente propone. Tú decides."** con `fade-up` | La partícula se detiene 0.6s en este arco (la compuerta humana) antes de seguir |
| Salida hacia la flecha | `clic 5` | `draw-line` del tramo final del anillo, que baja hacia la base, y del cuerpo de la flecha hasta la punta | 900ms, `firme` |
| Cloud-native | `clic 5` | `scale-in` del icono y la etiqueta + un brillo suave (`pulse`) | +900ms |
| Caja Legacy al final | `clic 5` | Se desatura (pasa a gris) | 600ms |
| Flujo continuo | `loop` (tras el clic 5) | `flow` | Partículas recorren Legacy → las 4 etapas → la flecha → Cloud-native; 4s por recorrido |

**Sin animación:**
- Al entrar se ven el núcleo con el logo, el título y la caja Legacy.
- Cada clic muestra su arco, su icono y su etiqueta completos, sin trazo progresivo.
- En el clic 5 aparecen la flecha y Cloud-native, y la caja Legacy queda en gris.
- Sin partículas.

**Reglas de legibilidad:**
- Etiquetas de etapa de 28px o más; descripciones de 22px o más.
- Los arcos con 6px de trazo, para que se vean en el proyector.
- El anillo y la flecha nunca tapan el logo.

**Notas del orador:**
> *(entra el logo)* Esta es la respuesta de AWS a todo eso que teníamos en la cabeza: AWS Transform, agentes de IA para
> migrar y modernizar. *(Legacy aparece y tiembla)* Partimos de aquí: sistemas legados, lentos, riesgosos y costosos de
> modernizar. *(clic)* Primero, el descubrimiento: identifica las aplicaciones y sus dependencias. *(clic)* Luego
> planifica la migración en oleadas. *(clic)* La IA refactoriza el código legado a código moderno. *(clic)* Y en cada
> paso crítico hay un humano que revisa y valida: el agente propone, tú decides. *(clic)* Así se llega a una
> arquitectura cloud-native: un negocio más rápido, más ágil e innovador. Ese es el viaje, y en un momento lo vamos a
> recorrer con nuestros propios agentes.

**Transición:** el anillo, la flecha y las etiquetas se desvanecen; el núcleo con el logo se queda en su sitio y es el
centro de la diapositiva 6.

---

### Diapositiva 6. ¿Qué incluye AWS Transform?

**Objetivo:** mostrar el alcance real del servicio y cerrar con las cifras oficiales de su primer año. Todo atribuido a
AWS; sin cifras propias.

**En pantalla**
- **Centro:** el mismo núcleo de la diapositiva 5 (logo oficial de AWS Transform), para dar continuidad.
- **Seis tarjetas en órbita** (en tres clics, de a dos), cada una con un icono de línea, un título y una línea:
  1. **Migración de infraestructura:** VMware, bare metal e híbrido; del descubrimiento y las oleadas a la red y el
     cutover.
  2. **Windows full-stack:** aplicaciones .NET, bases de datos SQL Server y procesos de despliegue.
  3. **Mainframe:** assessment, extracción de reglas de negocio y generación de código, trazables hasta el origen.
  4. **Transformaciones custom:** Java, Node.js y Python, más los cambios propios de tu organización.
  5. **Modernización continua** *(Preview)*: analiza la deuda técnica en muchos repositorios y la corrige (dependencias
     al final de su vida, vulnerabilidades).
  6. **Migración de cargas de IA:** evalúa dependencias de otros proveedores de IA y planifica la migración a Amazon
     Bedrock.
- **Fila de chips** bajo la órbita: **También:** Assessment · Migración de almacenamiento (FSx for NetApp ONTAP) ·
  Kiro · MCP · CLI
- **Franja de cifras** abajo, con el título pequeño **Su primer año, según AWS:**
  - **4.500 millones+** líneas de código procesadas
  - **Cientos de miles** de servidores migrados
  - **1,6 millones+** horas ahorradas a clientes
  - Fuente, pequeña: *AWS Migration & Modernization Blog, "One year. 4.5 billion lines of code. 1.6 million hours
    saved", 15 may. 2026*

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Núcleo (viene de la diapositiva 5) | `entrar` | Ya en su sitio | El halo con `pulse` en `loop` |
| Órbita (círculo punteado) | `entrar` | `draw-line` | 1000ms |
| Tarjetas 1 y 2 | `clic 1` | `scale-in` desde el núcleo hasta su posición en la órbita (`morph`) | 600ms, 120ms entre ambas |
| Tarjetas 3 y 4 | `clic 2` | Igual | — |
| Tarjetas 5 y 6 | `clic 3` | Igual; la etiqueta "Preview" de la tarjeta 5 con `fade-up` | — |
| Fila de chips | `clic 4` | `stagger` de `fade-up` | 70ms entre chips |
| La órbita se atenúa (al 50 %) | `clic 5` | Opacidad | 500ms: el foco pasa a las cifras |
| Cifra "4.500 millones+" | `clic 5` | `count-up` 0→4.500 | 1600ms, dígitos tabulares |
| "Cientos de miles" | `clic 5` | `blur-in` | +400ms |
| Cifra "1,6 millones+" | `clic 5` | `count-up` 0→1,6 | +800ms |
| Fuente | `clic 5` | `fade-up` | +1600ms |

**Sin animación:** el núcleo y la órbita visibles; cada clic muestra sus tarjetas en su posición final; el clic 5
muestra las cifras con su valor final.

**Reglas de legibilidad:**
- Títulos de tarjeta de 24px o más y descripciones de 18px o más.
- Cifras de 64px o más.
- En el proyector, recortar las descripciones antes que bajar el tamaño de la letra.

**Notas del orador:**
> ¿Y qué incluye? *(clic)* Migración de infraestructura (VMware, bare metal e híbrido) y la modernización full-stack de
> Windows: .NET y SQL Server. *(clic)* Mainframe, con reglas de negocio trazables hasta el código, y transformaciones
> custom para Java, Node y Python. *(clic)* Y dos novedades: la modernización continua, en preview, y la migración de
> cargas de IA hacia Bedrock. *(clic)* Además: assessment, migración de almacenamiento, y lo usas desde Kiro, por MCP o
> por línea de comandos.
> *(clic)* ¿Funciona? Según AWS, en su primer año procesó más de 4.500 millones de líneas de código, migró cientos de
> miles de servidores y ahorró más de 1,6 millones de horas a sus clientes. Distintos legados, el mismo principio:
> agentes especializados, con humanos al mando.

**Transición:** zoom hacia el núcleo, que se queda como L01 en el centro del diagrama de la diapositiva 7.

**Verificación (antes de cada charla):** el producto evoluciona rápido, así que revisar estas fuentes.

| Dato | Fuente | Nota |
|---|---|---|
| Las 8 áreas (las 6 tarjetas y los chips) | [Página de AWS Transform](https://aws.amazon.com/transform/) | Lista vigente al 20 oct. 2026. "Modernización continua" figura como Preview. |
| 4.500 millones+ líneas procesadas; cientos de miles de servidores; 1,6 millones+ horas | [AWS blog, 15 may. 2026 (Dr. Asa Kalavade)](https://aws.amazon.com/blogs/migration-and-modernization/aws-transform-one-year-milestone/) | Cifras reportadas por AWS. El blog dice "processed", sin precisar si son líneas analizadas o transformadas. Decir "según AWS". |
| Alternativa más reciente | [Página de AWS Transform](https://aws.amazon.com/transform/) | La página menciona el equivalente a 1.305 años-desarrollador ahorrados. Usar una sola fuente por diapositiva, para no mezclar períodos. |

---

### Diapositiva 7. ¿Por qué un mini-demo?

**Objetivo:** mostrar con un diagrama de arquitectura todo lo que AWS Transform integra en una migración real, y
de ahí justificar el mini-demo. La densidad del diagrama es intencional: comunica que un demo completo toma horas.

**Cómo se arma:**
- **Cajas de logo vacías:** cada caja tiene un número (L01…L23), una etiqueta y un espacio cuadrado para el logo, que
  pone el presentador.
- **Logos de AWS:** usar los iconos oficiales del paquete *AWS Architecture Icons*
  ([aws.amazon.com/architecture/icons](https://aws.amazon.com/architecture/icons/)).
- **Logos que no son de AWS** (VMware, GitHub/GitLab/Bitbucket/Azure Repos, Terraform): los de cada marca, solo como
  referencia.

**Caja de logo (componente):**
- Cuadrado de 96×96px con borde punteado de 2px, esquinas de 16px, fondo blanco y el texto gris claro "[logo]".
- Debajo, la etiqueta de la caja (18–20px).
- Las cajas se agrupan en contenedores redondeados con un título de grupo (16px, gris).

**Layout** (lienzo de 1920×1080)

| Zona | Grupo | Cajas |
|---|---|---|
| Arriba, centrado | Título | **Una migración real toca todo esto** |
| Centro (≈960, 540) | **AWS Transform** (núcleo, más grande: 180×180) | L01 AWS Transform |
| Arriba del núcleo | **Acceso y seguridad** | L02 AWS IAM Identity Center · L03 AWS IAM · L04 AWS KMS |
| Debajo del núcleo | **Herramientas de desarrollo** | L05 Kiro (MCP) · L06 AWS Transform CLI |
| Columna izquierda | **Origen** (no son de AWS) | L07 VMware vCenter · L08 Repositorios de código (GitHub, GitLab, Bitbucket, Azure Repos) · L09 Mainframe (COBOL) |
| Segunda columna | **Conectores y entrada** | L10 AWS Application Discovery Service · L11 AWS CodeConnections · L12 Amazon S3 |
| Columnas derechas, arriba | **Landing zone y red** | L13 AWS Organizations · L14 Landing Zone Accelerator on AWS · L15 Amazon VPC (subredes, Transit Gateway, NAT) |
| Columnas derechas, al medio | **Infraestructura como código** | L16 AWS CloudFormation · L17 AWS CDK · L18 Terraform |
| Columnas derechas, abajo | **Migración y cómputo** | L19 AWS Transform MGN (antes AWS Application Migration Service) · L20 Amazon EC2 · L21 Amazon ECS · L22 Amazon EKS |
| Junto a cómputo | **Datos** | L23 Amazon Aurora PostgreSQL |

**Conexiones** (flechas de 2–3px; las de seguridad punteadas):
- Origen → Conectores: L07 → L10 (descubrimiento) · L08 → L11 (código) · L09 → L12 (artefactos)
- Conectores → núcleo: L10, L11, L12 → L01
- Acceso y seguridad → núcleo: L02, L03, L04 → L01 (punteadas)
- Herramientas → núcleo: L05, L06 ↔ L01 (doble sentido)
- Núcleo → salidas: L01 → Landing zone y red · L01 → Infraestructura como código · L01 → L19 → L20 (rehost) · L01 → L21
  y L22 (contenedores) · L01 → L23 (base de datos)

**Insignia de conteo**, abajo a la derecha: **se integra con 17 servicios y herramientas de AWS**. Cuenta L02–L05,
L10–L17 y L19–L23; no cuenta el propio AWS Transform (L01), su CLI (L06), los orígenes (L07–L09) ni Terraform (L18).

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Título | `entrar` | `split-words` | — |
| Núcleo L01 | `entrar` | `scale-in` + halo con `pulse` en `loop` | Continúa desde el núcleo de la diapositiva 6 |
| Grupos Origen y Conectores | `clic 1` | `stagger` de `scale-in` (cajas) + `draw-line` (flechas hacia el núcleo) | 60ms entre cajas |
| Acceso y seguridad + Herramientas | `clic 2` | `stagger` de `fade-up` + `draw-line` punteada | 60ms entre cajas |
| Todas las salidas (derecha) | `clic 3` | `stagger` rápido de `scale-in` + `draw-line` de las flechas | 40ms entre cajas: debe sentirse como una "explosión" de servicios |
| Flujo por las flechas | `loop` (desde el clic 3) | `flow` | Partículas de izquierda a derecha, pasando por el núcleo |
| Insignia "se integra con 17 servicios y herramientas de AWS" | `clic 3` | `count-up` 0→17 + `stamp` | +800ms |
| El diagrama completo retrocede | `clic 4` | Desenfoque 0→8px + escala 1→0.9 + opacidad al 40 % | 700ms, `firme` |
| Texto "Un demo completo integra todo esto… y toma horas." | `clic 4` | `fade-up` | +300ms, centrado sobre el diagrama desenfocado |
| Caja "Mini-demo" | `clic 5` | `morph` desde el núcleo hasta el centro-derecha, nítida | 800ms |
| Contenido de la caja Mini-demo | `clic 5` | `stagger` de `fade-up` | "Lo construimos con Amazon Bedrock AgentCore" · "6 agentes · Strands · Amazon Nova y DeepSeek" |
| Nota honesta | `clic 6` | `fade-up` | *No es AWS Transform. Es una réplica transparente del loop que corren sus agentes.* |

**Sin animación:** cada clic muestra su grupo completo con sus flechas, sin partículas. El clic 4 deja el diagrama
desenfocado y atenuado; los clics 5 y 6 muestran la caja y la nota.

**Reglas de legibilidad:**
- El diagrama es denso a propósito, pero las etiquetas deben leerse antes del desenfoque: 18px como mínimo.
- Dejar 24px entre cajas.
- Nada de texto dentro de las flechas.

**Notas del orador:**
> En una migración real, AWS Transform no trabaja solo. *(clic)* Descubre tu VMware con Application Discovery Service,
> se conecta a tu código con CodeConnections y trabaja con artefactos en S3. *(clic)* Se gobierna con IAM Identity
> Center, IAM y KMS, y lo puedes usar desde Kiro o la línea de comandos. *(clic)* Y produce y orquesta todo esto:
> landing zone, red, infraestructura como código en CloudFormation, CDK o Terraform, rehost con Transform MGN,
> contenedores en ECS y EKS, y bases de datos en Aurora PostgreSQL. Se integra con diecisiete servicios y herramientas de AWS.
> *(clic)* Un demo completo de todo esto nos tomaría la tarde. *(clic)* Así que construimos un mini-demo que replica
> sus características con Amazon Bedrock AgentCore. *(clic)* Y quiero ser muy claro: esto no es AWS Transform. Es una
> réplica transparente, para que veamos qué pasa por dentro.

**Transición:** la caja Mini-demo hace un `morph` hacia la columna "Agentes" de la diapositiva 8.

**Verificación de las integraciones (antes de cada charla):**

| Integración | Fuente |
|---|---|
| IAM Identity Center (aplicación web), IAM, KMS | [FAQ de AWS Transform](https://aws.amazon.com/transform/faq/) · [Identity and access management](https://docs.aws.amazon.com/transform/latest/userguide/security-iam.html) · [Getting started](https://docs.aws.amazon.com/transform/latest/userguide/getting-started.html) |
| Application Discovery Service, Transform MGN (rehost) | [Simplify server migration with AWS Transform for VMware](https://aws.amazon.com/blogs/migration-and-modernization/simplify-server-migration-to-aws-with-aws-transform-for-vmware/) · [AWS Transform MGN User Guide](https://docs.aws.amazon.com/pdfs/mgn/latest/ug/user-guide.pdf) |
| CloudFormation, CDK, Terraform, LZA, OUs y cuentas destino; artefactos en S3 | [Generated network artifacts](https://docs.aws.amazon.com/transform/latest/userguide/transform-migrations-generated-artifacts.html) · [Terraform para VMware (sept. 2025)](https://aws.amazon.com/about-aws/whats-new/2025/09/aws-transform-terraform-vmware-network-automation/) · [AWS Transform for VMware](https://aws.amazon.com/transform/vmware/) |
| Contenedores en ECS (Terraform) y EKS (Helm) | [AWS Transform for migrations](https://aws.amazon.com/transform/migrations/) · [Release notes](https://docs.aws.amazon.com/transform/latest/userguide/transform-vmware-release-notes.html) |
| CodeConnections (GitHub, GitLab, Bitbucket, Azure Repos); SQL Server → Aurora PostgreSQL | [Modernize SQL Server to Aurora PostgreSQL](https://aws.amazon.com/blogs/dotnet/modernize-microsoft-sql-server-databases-to-aurora-postgresql-using-aws-transform/) · [.NET modernization](https://aws.amazon.com/transform/windows/net/) |
| Kiro y el servidor MCP, CLI | [Developer tools](https://docs.aws.amazon.com/transform/latest/userguide/developer-tools.html) · [AWS Transform for mainframe](https://aws.amazon.com/transform/mainframe/) · [AWS Transform custom](https://docs.aws.amazon.com/transform/latest/userguide/custom-get-started.html) |

No se incluye CloudTrail: no encontramos documentación de esa integración. Agregarlo solo si se verifica.

---

### Diapositiva 8. Una transformación. Tres stacks.

**Objetivo:** el mapa de la charla, legible en 3 segundos. Tres tarjetas grandes con logos (no cajas de texto), y la
del centro, los Agentes, como protagonista.

**En pantalla**
- Título en una sola línea: **Una transformación. Tres stacks.** ("Tres stacks" con el degradado de la plantilla)
- Tres tarjetas de igual alto. La del centro es un 8 % más grande, está elevada y tiene un brillo morado:

| | Legacy | Agentes (protagonista) | Target |
|---|---|---|---|
| Logos (casillas para el presentador) | EC2 · Flask · SQLite | Amazon Bedrock AgentCore · Strands Agents · Amazon Nova / DeepSeek | Kubernetes · Terraform · Kustomize |
| Título | **Legacy** (naranja-marrón) | **Agentes** (morado) | **Target** (azul) |
| Una línea | Lo que heredamos | Quién hace el trabajo | A dónde llegamos |
| Chip | monolito · disco local · a mano | 6 agentes · humano en cada paso | no-root · probado |
| Insignia | — | — | ✓ verde, en la esquina superior derecha de la tarjeta |

- Flechas gruesas (6px) en degradado, de Legacy a Agentes y de Agentes a Target, con partículas.
- Se elimina el check suelto en la esquina de la diapositiva, y la frase "Tres Stacks" ya no va abajo.

**Animaciones**

| Elemento | Disparador | Tipo | Parámetros |
|---|---|---|---|
| Título | `entrar` | `split-words`; "Tres stacks." con `highlight-sweep` del degradado | — |
| Tarjeta Legacy | `clic 1` | `stack-build` (sube) + `stagger` de los logos + `shake-soft` | — |
| Flecha 1 + tarjeta Agentes | `clic 2` | `draw-line` + `scale-in` hasta 1.08 con un brillo `pulse` | — |
| Flecha 2 + tarjeta Target | `clic 3` | `draw-line` + `scale-in` + `stamp` del ✓ | — |
| Partículas en las flechas | `loop` | `flow` | 3s |

**Sin animación:** cada clic muestra su tarjeta completa; sin partículas.

**Notas del orador:** las mismas de antes (Legacy → Agentes → Target).

**Transición:** la tarjeta Agentes hace un `morph` y se convierte en la barra de pestañas de la diapositiva 9.

---

### Diapositivas 9–14. Los seis agentes: sistema de diseño

**Principio:** cada diapositiva se entiende en 3 segundos con **una frase y un número**. La explicación va en las notas
del orador, no en la pantalla. Máximo 30 palabras por diapositiva (sin contar las pestañas ni el pie).

**Anatomía, igual en las seis:**
1. **Pestañas** arriba (como ya están), con la del agente actual resaltada.
2. **Antetítulo** pequeño en mayúsculas moradas: `AGENTE N · NOMBRE`.
3. **Titular** grande (≥ 60px, negrita, negro), una frase de 6 palabras como máximo.
4. **La máquina**, en el centro y a todo lo ancho: **Entrada** (izquierda, icono + 2–4 palabras) → **visual del agente**
   (centro, el protagonista) → **Salida** (derecha, icono + 2–4 palabras). Flechas gruesas entre las zonas.
5. **Dos pastillas** bajo el visual, siempre con el mismo color:
   - **Herramientas** en azul (`#2B35E6`): lo que hace el código, de forma determinista.
   - **Modelo** en morado (`#6A1FD0`): lo que decide la IA.
   Cada una con 4 palabras como máximo.
6. **Cifra protagonista** abajo (≥ 56px), con `count-up`.
7. **Etiqueta discreta** a la derecha, sobre la onda del pie: `En AWS Transform → …` (16–18px).

**Código de color, igual en todas:**
- azul = herramientas
- morado = modelo
- verde = humano / aprobado
- rojo y naranja = problemas

Nada de bloques sin etiqueta: cada forma del visual dice qué es.

**Animación común (3 clics):**
- `entrar`: el resaltado de la pestaña hace un `morph` desde la anterior; antetítulo y titular con `split-words`; la
  Entrada con `fade-up`.
- `clic 1`: el visual del agente trabaja (su animación propia, abajo).
- `clic 2`: la Salida con `scale-in` y las dos pastillas con `stagger` de `fade-up`.
- `clic 3`: la cifra protagonista con `count-up` + `stamp`, y la etiqueta de AWS Transform con `fade-up`.

**Contenido por agente:**

| # | Titular | Entrada | Visual del agente (clic 1) | Salida | Herramientas | Modelo | Cifra | En AWS Transform |
|---|---|---|---|---|---|---|---|---|
| 9 · Descubrimiento | **Encuentra lo que nadie documentó.** | Código · Terraform · EC2 | El radar sobre las 4 tarjetas de archivo (`app.py`, `schema.sql`, `legacy-inventory.service`, `main.tf`) y los pines que caen: rojo = crítico, naranja = alto. Se mantiene el visual actual | Evaluación (chip "Replatform") | escaneo · Terraform · CloudWatch | severidad · estrategia 6R | **17 hallazgos · 3 críticos** | Discovery & Assessment |
| 10 · Requisitos | **Convierte un documento en un contrato.** | `migration-requirements.md` | 4 líneas resaltadas del documento vuelan a 4 filas concretas de la especificación: **SLO 99,9 %**, **US$450/mes**, **Graviton (arm64)**, **PostgreSQL en RDS**. Debajo, 3 burbujas "?"; un avatar humano toca 2 y pasan a "✓ resuelta"; la tercera queda "? cutover" | Especificación validada | plantilla · validador | prioridades · supuestos | **24 requisitos · 2 de 3 dudas resueltas por ti** | Objetivo del job + humano en el loop |
| 11 · Modernización | **No reescribe: adapta un patrón probado.** | Monolito Flask (`app.py · 2017`) | El bloque del monolito se fractura y se rearma como un contenedor de **dos capas con nombre**. Abajo, la capa ancha azul **Patrón probado**, con 4 mini chips: Dockerfile no-root · /healthz · logs JSON · Kustomize. Arriba, la capa delgada morada **Lógica de negocio · rutas /api**. Se eliminan los bloques grises sin etiqueta | Microservicio + manifiestos | patrón · pruebas · validación | porta rutas · escribe pruebas | **23/23 pruebas · inyección SQL corregida** | Transformación de código y contenedores |
| 12 · Terraform | **Infraestructura válida al primer intento.** | Especificación | Dos paneles. **Desde cero**: líneas de HCL y un sello rojo "✗ inválido". **Con blueprint**: una cuadrícula azul que se llena con 4 chips (us-west-2 · Graviton t4g · Multi-AZ · **Kubernetes 1.33**) y un sello verde "✓ terraform validate". El chip "1.33" late suave (es la semilla del "aha") | VPC · EKS · ECR · RDS | blueprint · terraform validate | mapea la especificación | **17/17 verificaciones ✓** | Red e infraestructura como código |
| 13 · Validación K8s | **No lo valida en papel: lo despliega.** | Manifiestos | 4 líneas de YAML en rojo (`:latest` · sin límites · FS escribible · sin PDB) pasan a verde. Luego aparece un anillo de clúster con 3 pods y la fila de pruebas de humo **200 · 200 · 200 · 201 · 409**, con el 409 etiquetado "sobreventa bloqueada ✓" | Pods en Kubernetes | kubeconform · 16 políticas · kind | corrige violaciones | **4 violaciones → 0** | Pruebas y validación |
| 14 · FinOps | **Encontró US$365/mes que nadie vio.** | Terraform + manifiestos | Tres barras y una línea punteada en $450. **Hoy $72** (gris). **Diseñado $623**, con el segmento rojo "+$365 · Kubernetes 1.33 en soporte extendido". **Optimizado $208** (índigo) con un ✓ | Plan de costos | motor de costos · precios AWS | elige optimizaciones | **US$72 → 623 → 208** | Assessment y caso de negocio |

**Notas del orador** (la explicación detallada se dice en voz alta; ya no se lee en pantalla):

- **9 · Descubrimiento:**
  > El primer agente encuentra lo que nadie documentó. *(clic)* Las herramientas escanean el código, el Terraform y la
  > EC2: rutas, secretos en el código, SQL concatenado, reportes en disco. *(clic)* El modelo solo pondera la severidad
  > y elige la estrategia. *(clic)* Diecisiete hallazgos, tres críticos, en unos diez segundos, y cada uno se puede
  > auditar.
- **10 · Requisitos:**
  > Este agente convierte un documento en un contrato. *(clic)* Cada línea del negocio se vuelve un valor concreto:
  > 99,9 % de disponibilidad, 450 dólares al mes, Graviton, PostgreSQL. No inventa respuestas: deja tres dudas
  > abiertas. En el demo le respondo en lenguaje natural, y resuelve dos. *(clic)* La herramienta valida; el modelo
  > prioriza y propone supuestos. *(clic)* Veinticuatro requisitos, y dos de tres dudas resueltas por un humano.
- **11 · Modernización:**
  > No reescribe desde cero: adapta un patrón probado. *(clic)* El monolito se convierte en un contenedor. Abajo, un
  > patrón probado: Dockerfile no-root, health checks, logs JSON, Kustomize. Arriba, lo único que requiere criterio: la
  > lógica de negocio de las rutas. *(clic)* Las herramientas montan el patrón y corren las pruebas; el modelo porta
  > las rutas y escribe las pruebas. *(clic)* Veintitrés de veintitrés, y sin inyección SQL.
- **12 · Terraform:**
  > Primero dejamos que los modelos escribieran esto desde cero, y todos fallaron. *(clic)* Con un blueprint validado,
  > pasan al primer intento. Fíjense en un detalle: Kubernetes 1.33. Guárdenlo. *(clic)* El blueprint y terraform
  > validate son herramientas; el modelo solo mapea la especificación. *(clic)* Diecisiete de diecisiete
  > verificaciones.
- **13 · Validación K8s:**
  > Este agente no valida en papel: despliega. *(clic)* Alguien editó los manifiestos a mano; detecta las cuatro
  > violaciones y las corrige. Luego despliega en un Kubernetes real y corre pruebas de humo, y el 409 es la sobreventa
  > bloqueada. *(clic)* kubeconform, dieciséis políticas y kind son herramientas; el modelo decide cómo corregir.
  > *(clic)* De cuatro violaciones a cero.
- **14 · FinOps:**
  > Y la pregunta de todo CFO. *(clic)* Hoy, 72 dólares al mes. El diseño, 623, por encima del presupuesto. ¿Por qué?
  > ¿Recuerdan el 1.33? Está en soporte extendido de EKS: 365 dólares al mes que nadie vio. Optimizado: 208, dentro del
  > presupuesto. *(clic)* Cada número sale del motor de costos; el modelo solo elige qué optimizar y lo explica.
  > *(clic)* Y seamos honestos: sigue costando unas tres veces más que un servidor. Lo que se paga es alta
  > disponibilidad, Multi-AZ, autoescalado y seguridad.

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
