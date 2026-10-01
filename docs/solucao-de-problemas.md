# Solução de problemas comuns

Erros reais encontrados ao rodar `migrate.py`/`validate_migration.py` numa VM,
na ordem em que costumam aparecer numa instalação nova: primeiro permissões de
diretório, depois configuração do `.env`, depois credenciais do GCP, depois
permissões do BigQuery. Cada seção traz o sintoma exato, a causa e o comando
que resolve.

Se o erro que você está vendo não está aqui, confira antes se `git pull` na VM
está em dia — vários destes já foram corrigidos no próprio script.

## 1. `PermissionError: [Errno 13] Permission denied: '/opt/migracao/...'`

Sintoma:

```text
PermissionError: [Errno 13] Permission denied: '/opt/migracao/validation'
```

O mesmo acontece com `/opt/migracao/checkpoints`, `/opt/migracao/logs`, ou
qualquer diretório apontado por `VALIDATION_OUTPUT_DIR`, `CHECKPOINT_FILE` etc.

**Causa:** `/opt/migracao` é `root:postgres` modo `750` — o usuário `postgres`
consegue ler e atravessar, mas não criar arquivos/pastas novas ali. Se o
diretório de saída não foi criado antecipadamente com dono `postgres`, o
script falha ao tentar criá-lo na primeira execução.

**Correção:** crie o diretório com o dono certo (troque o nome pelo diretório
do erro):

```bash
sudo install -d -o postgres -g postgres -m 750 /opt/migracao/validation
```

Se não souber qual caminho está configurado, confira antes:

```bash
grep -E "CHECKPOINT_FILE|VALIDATION_OUTPUT_DIR" /opt/migracao/.env /opt/migracao/.env.validacao
```

## 2. `404 ... Project seu-projeto-gcp is not found`

Sintoma:

```text
google.api_core.exceptions.NotFound: 404 POST .../projects/seu-projeto-gcp/datasets...
Project seu-projeto-gcp is not found. Make sure it references valid GCP project...
```

**Causa:** `BQ_PROJECT` (ou `--bq-report-table`/`VALIDATION_BQ_TABLE`) ainda
usa o valor de exemplo `seu-projeto-gcp` de `config/examples/*.example`, em
vez do Project ID real do GCP.

**Correção:** confira e corrija no `.env`/`.env.validacao`:

```bash
grep BQ_PROJECT /opt/migracao/.env.validacao
```

Com o código atualizado, esse valor é recusado localmente antes de qualquer
chamada ao BigQuery, com uma mensagem apontando exatamente a variável errada
— em vez do 404 confuso.

## 3. `DefaultCredentialsError: File  was not found.`

Sintoma (repare no espaço duplo — nome de arquivo vazio):

```text
google.auth.exceptions.DefaultCredentialsError: File  was not found.
```

**Causa:** `GOOGLE_APPLICATION_CREDENTIALS` está definida no ambiente como
string **vazia** (não ausente) — comum ao usar `sudo -E`, que herda todo o
ambiente do usuário chamador, inclusive uma variável exportada vazia sem
querer. `load_dotenv()` não sobrescreve uma variável já definida no processo,
então um `.env` corretamente vazio/comentado não resolve sozinho.

**Correção:** evite `sudo -E`; rode removendo a variável explicitamente:

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  /opt/migracao/.venv/bin/python /opt/migracao/migrate.py
```

Com o código atualizado, os dois scripts também tratam essa variável vazia
como se não estivesse definida, então isso deixa de depender de lembrar do
`-u` toda vez.

## 4. `403 ... does not have bigquery.datasets.create permission`

Sintoma:

```text
google.api_core.exceptions.Forbidden: 403 POST .../datasets...
Access Denied: Project ...: User does not have bigquery.datasets.create permission in project ...
```

**Causa:** a Service Account da VM não tem permissão de criar datasets novos
no projeto BigQuery. Com o código atualizado, `migrate.py` só pede essa
permissão quando o dataset realmente não existe ainda (antes, pedia sempre,
mesmo para datasets já existentes — atualize o código na VM primeiro). Se o
erro persistir depois de atualizar, o dataset daquele banco específico
realmente não existe e a conta atual não pode criá-lo.

**Correção — pelo console do GCP:**

1. Descubra a Service Account da VM (rode na própria VM):

   ```bash
   curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
   ```

2. Acesse `console.cloud.google.com/iam-admin/iam?project=SEU-PROJETO` (troque
   pelo Project ID real).
3. Clique em **"Conceder acesso"**.
4. Em **"Novos principais"**, cole o e-mail da Service Account do passo 1.
5. Em **"Selecionar um papel"**, escolha **`BigQuery Data Editor`**
   (`roles/bigquery.dataEditor`) e salve.

Esse papel é concedido no nível do **projeto** — não existe uma tela de
permissão "no dataset" para um dataset que ainda não foi criado. Se preferir
algo mais restrito, dá para criar um papel personalizado só com
`bigquery.datasets.create` em IAM → Papéis → Criar papel, mas para uma conta
de automação interna o `BigQuery Data Editor` já é razoável.

## 5. `Could not convert JSON value to geography` num polígono

Duas mensagens diferentes, mesma família de causa: o `geometry` do PostGIS
não garante a **estrutura** (qual anel é a casca externa) nem a
**orientação** (sentido horário ou anti-horário) dos anéis de um polígono —
qualquer uma das duas é aceita como válida pelo PostGIS. O `GEOGRAPHY` do
BigQuery é esférico e exige as duas coisas corretas.

### 5a. `... overlap area larger than hemisphere. Check if the polygon orientation is correct`

```text
Could not convert JSON value to geography: Multipolygon contains polygons with
overlap area larger than hemisphere. Check if the polygon orientation is correct.
Field: geom; Value: MULTIPOLYGON(...)
```

Orientação errada: com o anel invertido, um polígono pequeno é interpretado
como "todo o planeta menos ele" — área maior que um hemisfério.

### 5b. `Polygon's first loop must be shell. It is nested in loop N`

```text
Could not convert JSON value to geography: Polygon's first loop must be shell.
It is nested in loop 2 Field: geom; Value: MULTIPOLYGON(...)
```

Estrutura errada: o primeiro anel (que deveria ser a casca externa) está
geometricamente dentro de outro anel (que deveria ser um buraco). `ST_MakeValid`,
usado para corrigir geometrias inválidas, pode reconstruir os anéis sem
garantir que o primeiro seja o externo.

**Correção (cobre as duas):** já corrigida no código — `migrate.py`
reconstrói a estrutura casca/buraco com `ST_BuildArea` (só em colunas cujo
tipo original é polígono/multipolígono — em ponto/linha isso zeraria os
dados, então não é aplicado), normaliza a orientação com `ST_ForceRHR` e,
por fim, **inverte** o resultado com `ST_Reverse`. Esse último passo foi
confirmado em produção: `ST_ForceRHR` sozinho não mudava o WKT de nenhuma
linha (a geometria já estava "correta" pela convenção do PostGIS), e o
BigQuery continuava recusando todas — ou seja, a convenção de "regra da mão
direita" do PostGIS é o **oposto** da que o BigQuery GEOGRAPHY espera.
Atualize o código na VM e remigre só a tabela afetada:

```bash
sudo git -C /opt/migracao pull --ff-only origin main
```

```bash
sudo -u postgres env PG_DATABASE="nome_do_banco" \
  /opt/migracao/.venv/bin/python /opt/migracao/migrate.py \
  --tables "schema.tabela"
```

Dados espaciais reais podem ter outras formas de geometria malformada além
dessas duas. Se aparecer uma mensagem de erro do BigQuery para GEOGRAPHY
diferente destas, mande o log — a causa provavelmente é nova e exige
investigação específica, não é automaticamente coberta pela correção acima.

## 6. `Invalid field name "..."`

```text
400 POST .../tables?prettyPrint=false: Invalid field name "nº_boletim".
Fields must contain the allowed characters, and be at most 300 characters long.
```

**Causa:** o PostgreSQL aceita quase qualquer nome de coluna entre aspas
(espaço, acento, parênteses, `/`, `;`, ponto etc.) — comum em tabelas
importadas de planilha. O BigQuery não aceita. A tabela inteira falha ao
criar se **uma só** coluna tiver um nome fora do padrão.

**Correção:** já corrigida no código — cada nome de coluna é normalizado
(mesma regra usada em nomes de tabela/dataset) antes de criar o schema e a
consulta no PostgreSQL já devolve as linhas com essas chaves. Duas colunas
que colidirem no nome normalizado (ex.: `"Peso Infraest."` e
`"Peso_Infraest."` viram as duas `Peso_Infraest`) interrompem a tabela antes
da carga, em vez de uma sobrescrever silenciosamente a outra — nesse caso
raro, renomeie uma das colunas na origem antes de remigrar. Atualize o
código na VM e remigre a tabela afetada:

```bash
sudo git -C /opt/migracao pull --ff-only origin main
```

```bash
sudo -u postgres env PG_DATABASE="nome_do_banco" \
  /opt/migracao/.venv/bin/python /opt/migracao/migrate.py \
  --tables "schema.tabela"
```

## 7. `ST_Transform: Input geometry has unknown (0) SRID`

Não é bug — é configuração ausente. Alguma tabela tem geometria sem SRID
definido (`SRID 0`) no PostgreSQL, e `ST_Transform` se recusa a reprojetar
sem saber de onde partir.

**Correção:** defina `PG_DEFAULT_SRID` no `.env` com o SRID real da origem
(ex.: `31983` para SIRGAS 2000 / UTM 23S, comum em dados municipais
brasileiros — confirme o SRID correto com quem gerou os dados). Com a
variável definida, o script assume esse SRID só para geometrias com SRID 0,
sem alterar as que já têm SRID correto:

```dotenv
PG_DEFAULT_SRID=31983
```

## 8. `429 Exceeded rate limits: too many table update operations for this table`

Não é bug — é cota do BigQuery (limite de operações de metadado por tabela
numa janela de tempo), atingida ao processar muitas tabelas do mesmo dataset
em sequência numa migração grande.

**Correção:** normalmente basta remigrar só a(s) tabela(s) que falharam,
depois que a janela de cota esvaziar (alguns minutos):

```bash
sudo -u postgres env PG_DATABASE="nome_do_banco" \
  /opt/migracao/.venv/bin/python /opt/migracao/migrate.py \
  --tables "schema.tabela"
```

Se o mesmo erro persistir com frequência, aumente o espaçamento entre
tentativas para essa execução:

```dotenv
MAX_RETRIES=5
RETRY_DELAY_SECONDS=5
```

## Depois de qualquer uma dessas correções

Atualize o código na VM e rode de novo:

```bash
sudo git -C /opt/migracao pull --ff-only origin main
```

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  /opt/migracao/.venv/bin/python /opt/migracao/migrate.py
```

## Isto não é um erro do script: divergências no relatório de validação

`DESTINATION_NOT_FOUND`, `SCHEMA_MISMATCH`, `ROW_MISMATCH` etc. no relatório
de `validate_migration.py` não são falhas de execução — são o resultado
esperado quando uma tabela precisa ser (re)migrada. Cada linha do relatório
já traz `status_description` (explicação em português) e `fix_command` (o
comando pronto para corrigir aquela tabela específica). Veja
[docs/validacao.md](validacao.md) para a lista completa de status possíveis.
