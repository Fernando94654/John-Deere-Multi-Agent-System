int COLS = 20;
int ROWS = 15;
int CELL = 35;

int[][] campo; // 0 = listo para cosechar, 1 = cosechado
boolean[][] reservado; // evita que dos tractores vayan a la misma celda

ArrayList<Tractor> tractores = new ArrayList<Tractor>();
ArrayList<Contenedor> contenedorres = new ArrayList<Contenedor>();
Silo silo;

float distanciaTotal = 0;
float combustibleTotalConsumido = 0;
int celdasCosechadas = 0;
int totalCeldas;

// ---------------- ECONOMÍA / PRESUPUESTO ----------------
// Idea general: antes de arrancar, con el presupuesto inicial se decide
// cuántos tractores comprar. Esa decisión deja "apartado" el dinero
// necesario para operar (agua + combustible) durante toda la cosecha,
// y el resto se usa para maquinaria. Mientras corre la simulación, cada
// riego y cada recarga de combustible descuenta del presupuesto real.

float presupuestoInicial = 15000;   // budget total disponible
float presupuestoActual;             // lo que va quedando en vivo

float costoPorTractor = 1200;        // costo de compra de cada tractor
int maxTractoresPosibles = 8;        // techo razonable de flota

float costoPorContenedor=1000;
int maxContenedoresPosibles=8;

float costoAguaPorCelda = 3;         // $ por regar 1 celda antes de cosecharla
float costoCombustiblePorUnidad = 8; // $ por cada 1% de combustible comprado
float combustiblePorCeldaEstimado = 3; // % de combustible estimado por celda (para el cálculo previo)

float dineroGastadoMaquinaria = 0;
float dineroGastadoCombustible = 0;
float dineroGastadoAgua = 0;

int numContenedoresOptimo;
int numTractoresOptimo;
boolean presupuestoAgotado = false;

void setup() {
  size(700, 560);
  totalCeldas = COLS * ROWS;
  campo = new int[COLS][ROWS];
  reservado = new boolean[COLS][ROWS];

  for (int i = 0; i < COLS; i++) {
    for (int j = 0; j < ROWS; j++) {
      campo[i][j] = 0; // todo listo para cosechar al inicio
    }
  }

  silo = new Silo(COLS / 2, ROWS / 2);

  presupuestoActual = presupuestoInicial;

  // 1) Decidir la mejor combinación de tractores dado el budget
  numTractoresOptimo = calcularCombinacionOptima();
  
  numContenedoresOptimo = max(numTractoresOptimo / 2, 1);
  numContenedoresOptimo = constrain(numContenedoresOptimo, 1, maxContenedoresPosibles);

  // 2) Comprar esa flota (gasto de maquinaria, se descuenta una sola vez)
  dineroGastadoMaquinaria = numTractoresOptimo * costoPorTractor;
  presupuestoActual -= dineroGastadoMaquinaria;

  // 3) Repartir tractores en posiciones iniciales distribuidas por el borde
  for (int k = 0; k < numTractoresOptimo; k++) {
    int gx = int(map(k, 0, max(1, numTractoresOptimo - 1), 0, COLS - 1));
    int gy = (k % 2 == 0) ? 0 : ROWS - 1; // alterna arriba/abajo
    tractores.add(new Tractor(gx, gy));
  }
  
  for (int k = 0; k < numContenedoresOptimo; k++) {
    int gx = int(map(k, 0, max(1, numContenedoresOptimo - 1), 2, COLS - 3));
    int gy = ROWS / 2;
    contenedorres.add(new Contenedor(gx, gy));
  }
}

void draw() {
  background(235, 225, 200);
  dibujarGrilla();
  silo.mostrar();

  for (Tractor t : tractores) {
    t.actualizar();
    t.mostrar();
  }

  presupuestoAgotado = presupuestoActual <= 0;

  dibujarHUD();
  dibujarPanelPresupuesto();
}

// ---------------- OPTIMIZACIÓN DE PRESUPUESTO ----------------
// Estrategia simple tipo "greedy": el costo operativo (agua + combustible
// para cosechar todo el campo) es prácticamente el mismo sin importar
// cuántos tractores trabajen en paralelo (es la misma cantidad de celdas,
// el mismo riego, más o menos el mismo combustible total gastado).
// Lo único que cambia con más tractores es que se cosecha más rápido.
// Entonces: se reserva primero el dinero para operar TODO el campo,
// y con lo que sobra se compra la mayor cantidad de tractores posible.
int calcularCombinacionOptima() {
  float costoAguaTotalEstimado = totalCeldas * costoAguaPorCelda;
  float costoCombustibleTotalEstimado = totalCeldas * combustiblePorCeldaEstimado * costoCombustiblePorUnidad;
  float costoOperativoEstimado = costoAguaTotalEstimado + costoCombustibleTotalEstimado;

  float dineroParaMaquinaria = presupuestoInicial - costoOperativoEstimado;

  int n = floor(dineroParaMaquinaria / costoPorTractor);
  n = constrain(n, 1, maxTractoresPosibles); // siempre al menos 1 tractor

  println("== Cálculo de combinación óptima ==");
  println("Costo operativo estimado (agua+combustible): $" + nf(costoOperativoEstimado, 0, 2));
  println("Dinero disponible para maquinaria: $" + nf(dineroParaMaquinaria, 0, 2));
  println("Tractores a comprar: " + n + " (costo: $" + (n * costoPorTractor) + ")");

  return n;
}

// ---------------- CAMPO ----------------
void dibujarGrilla() {
  for (int i = 0; i < COLS; i++) {
    for (int j = 0; j < ROWS; j++) {
      if (campo[i][j] == 0) {
        fill(196, 165, 74); // listo para cosechar
      } else {
        fill(120, 90, 40); // ya cosechado
      }
      stroke(90, 70, 30);
      rect(i * CELL, j * CELL, CELL, CELL);
    }
  }
}

// ---------------- SILO ----------------
class Silo {
  float x, y;
  int gx, gy;

  Silo(int gx_, int gy_) {
    gx = gx_;
    gy = gy_;
    x = gx * CELL + CELL / 2;
    y = gy * CELL + CELL / 2;
  }

  void mostrar() {
    fill(30, 30, 30);
    noStroke();
    rectMode(CENTER);
    rect(x, y, CELL * 0.8, CELL * 0.8);
    rectMode(CORNER);
    fill(255);
    textAlign(CENTER, CENTER);
    textSize(10);
    text("SILO", x, y);
  }
}

// ---------------- TRACTOR ----------------
class Tractor {
  float x, y;       // posicion en pixeles
  int gx, gy;        // posicion en celdas
  float velocidad = 2.0;
  float carga = 0;
  float capacidadMax = 5;
  float combustible = 100;
  float radioSensor = CELL * 2.5;
  color col = color(46, 139, 87);
  PVector destinoPx = null;
  int destinoGX = -1, destinoGY = -1;
  boolean regresandoABase = false;

  Tractor(int gx_, int gy_) {
    gx = gx_;
    gy = gy_;
    x = gx * CELL + CELL / 2;
    y = gy * CELL + CELL / 2;
  }

  void actualizar() {
    // Sin presupuesto no hay operación: ni riego, ni combustible, ni movimiento.
    if (presupuestoAgotado) return;

    // Combustible bajo -> regresar al contenedor/base
    if (combustible < 15 && !regresandoABase) {
      regresandoABase = true;
      liberarReserva();
      destinoPx = new PVector(silo.x, silo.y);
      destinoGX = -1;
    }

    if (destinoPx == null && !regresandoABase) {
      buscarSiguienteParcela();
    }

    moverHaciaDestino();
    evitarColisiones();
  }

  // Asignacion: heuristica simple de cercania a la parcela lista mas proxima
  void buscarSiguienteParcela() {
    float mejorDist = Float.MAX_VALUE;
    int mgx = -1, mgy = -1;
    for (int i = 0; i < COLS; i++) {
      for (int j = 0; j < ROWS; j++) {
        if (campo[i][j] == 0 && !reservado[i][j]) {
          float d = dist(gx, gy, i, j);
          if (d < mejorDist) {
            mejorDist = d;
            mgx = i;
            mgy = j;
          }
        }
      }
    }
    if (mgx != -1) {
      reservado[mgx][mgy] = true; // coordinacion: reservo esta celda
      destinoGX = mgx;
      destinoGY = mgy;
      destinoPx = new PVector(mgx * CELL + CELL / 2, mgy * CELL + CELL / 2);
    }
  }

  void moverHaciaDestino() {
    if (destinoPx == null) return;

    PVector pos = new PVector(x, y);
    PVector dir = PVector.sub(destinoPx, pos);
    float d = dir.mag();

    if (d < 2) {
      // llegamos
      if (regresandoABase) {
        carga = 0;
        recargarCombustible();
        regresandoABase = false;
        destinoPx = null;
      } else {
        // hay que regar antes de cosechar; si no alcanza el presupuesto, se cancela la celda
        if (pagarAgua()) {
          campo[destinoGX][destinoGY] = 1; // celda cosechada
          celdasCosechadas++;
          carga += 1;
          gx = destinoGX;
          gy = destinoGY;
        } else {
          liberarReserva(); // no se pudo pagar el riego, se libera la celda
        }
        destinoPx = null;

        if (carga >= capacidadMax) {
          regresandoABase = true;
          destinoPx = new PVector(silo.x, silo.y);
        }
      }
    } else {
      dir.normalize();
      dir.mult(velocidad);
      x += dir.x;
      y += dir.y;
      distanciaTotal += velocidad;
      combustible -= 0.05;
      combustibleTotalConsumido += 0.05;
      combustible = max(combustible, 0);
    }
  }

  // Descuenta del presupuesto el costo de regar una celda antes de cosecharla.
  // Devuelve true si se pudo pagar, false si ya no alcanza el presupuesto.
  boolean pagarAgua() {
    if (presupuestoActual < costoAguaPorCelda) {
      return false;
    }
    presupuestoActual -= costoAguaPorCelda;
    dineroGastadoAgua += costoAguaPorCelda;
    return true;
  }

  // Recarga combustible según lo que el presupuesto restante alcance a pagar.
  void recargarCombustible() {
    float faltante = 100 - combustible;
    float costoRecargaCompleta = faltante * costoCombustiblePorUnidad;

    if (presupuestoActual >= costoRecargaCompleta) {
      combustible = 100;
      presupuestoActual -= costoRecargaCompleta;
      dineroGastadoCombustible += costoRecargaCompleta;
    } else {
      // solo alcanza para una recarga parcial
      float unidadesComprables = presupuestoActual / costoCombustiblePorUnidad;
      combustible += unidadesComprables;
      combustible = min(combustible, 100);
      dineroGastadoCombustible += presupuestoActual;
      presupuestoActual = 0;
    }
  }

  // Sensor de proximidad muy simple: si otro tractor esta muy cerca, frena un frame
  void evitarColisiones() {
    for (Tractor otro : tractores) {
      if (otro == this) continue;
      float d = dist(x, y, otro.x, otro.y);
      if (d < CELL * 0.6) {
        // pequeno "esquive": nudge lateral
        x += random(-1, 1);
        y += random(-1, 1);
      }
    }
  }

  void liberarReserva() {
    if (destinoGX != -1 && destinoGX < COLS && destinoGY < ROWS) {
      reservado[destinoGX][destinoGY] = false;
    }
  }

  void mostrar() {
    // radio de sensor (debug visual)
    noFill();
    stroke(col, 60);
    ellipse(x, y, radioSensor * 2, radioSensor * 2);

    fill(col);
    noStroke();
    ellipse(x, y, CELL * 0.6, CELL * 0.6);

    fill(255);
    textAlign(CENTER, CENTER);
    textSize(9);
    text(int(combustible) + "%", x, y - CELL * 0.6);
  }
}

//-----------------CONTENEDOR---------------
class Contenedor {
  float x, y;       // posicion en pixeles
  int gx, gy;        // posicion en celdas
  float velocidad = 2.0;
  float carga = 0;
  float capacidadMax = 10;
  float combustible = 100;
  float radioSensor = CELL * 2.5;
  color col = color(245, 188, 66);
  PVector destinoPx = null;
  int destinoGX = -1, destnoGY = -1;
  boolean regresandoABase = false;

  Contenedor(int gx_, int gy_) {
    gx = gx_;
    gy = gy_;
    x = gx * CELL + CELL / 2;
    y = gy * CELL + CELL / 2;
  }
}

// ---------------- HUD ----------------
void dibujarHUD() {
  fill(0, 150);
  noStroke();
  rect(0, height - 60, width, 60);

  fill(255);
  textAlign(LEFT, CENTER);
  textSize(12);
  float pct = 100.0 * celdasCosechadas / totalCeldas;
  text("Distancia total: " + int(distanciaTotal) + " px", 10, height - 40);
  text("Combustible consumido: " + nf(combustibleTotalConsumido, 0, 1), 10, height - 20);
  text("Cosecha: " + nf(pct, 0, 1) + "%   (" + celdasCosechadas + "/" + totalCeldas + " parcelas)", 300, height - 30);

  if (presupuestoAgotado) {
    fill(220, 60, 60);
    textAlign(CENTER, CENTER);
    textSize(14);
    text("PRESUPUESTO AGOTADO — operación detenida", width / 2, height - 40);
  }
}

// Panel superior con el desglose del presupuesto
void dibujarPanelPresupuesto() {
  int panelW = 230;
  int panelH = 95;
  int px = width - panelW - 10;
  int py = 10;

  fill(0, 160);
  noStroke();
  rect(px, py, panelW, panelH, 6);

  fill(255);
  textAlign(LEFT, TOP);
  textSize(11);
  float lh = 15;
  text("Budget inicial: $" + nf(presupuestoInicial, 0, 0), px + 10, py + 8);
  text("Presupuesto actual: $" + nf(max(presupuestoActual, 0), 0, 0), px + 10, py + 8 + lh);
  text("Tractores comprados: " + numTractoresOptimo + " ($" + nf(dineroGastadoMaquinaria, 0, 0) + ")", px + 10, py + 8 + lh * 2);
  text("Gasto combustible: $" + nf(dineroGastadoCombustible, 0, 0), px + 10, py + 8 + lh * 3);
  text("Gasto agua: $" + nf(dineroGastadoAgua, 0, 0), px + 10, py + 8 + lh * 4);
}
