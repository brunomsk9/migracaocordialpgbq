# Guia da VM: instalar Git, atualizar e rodar somente a validação

Execute os comandos no **terminal da VM aberto pelo navegador**. O projeto fica
em `/opt/migracao`. Repita a configuração em cada VM, usando sua porta PostgreSQL
e seu projeto BigQuery.

**Para apenas conferir a migração, execute `validate_migration.py`.** Não é
necessário executar `migrate.py`. O validador consulta PostgreSQL e BigQuery,
compara tabelas e gera um relatório; ele não recarrega as tabelas migradas.

## 1. Instalar o Git

Descubra a distribuição:

```bash
cat /etc/os-release
```

**Ubuntu ou Debian:**

```bash
sudo apt update
sudo apt install -y git
```

**Rocky Linux, AlmaLinux ou RHEL com DNF:**

```bash
sudo dnf install -y git
```

Confirme:

```bash
git --version
```

Deve aparecer algo como `git version 2.x.x`. Se já estiver conectado como `root`,
pode omitir `sudo`. Os comandos abaixo usam `root` para instalar/atualizar o
código e o usuário `postgres` para executar a validação.

## 2. Colocar o código em /opt/migracao

Escolha **somente um** dos casos abaixo. Não substitua a pasta enquanto houver
migração ou validação em execução.

### Caso A — a pasta já foi clonada pelo Git

Veja o repositório e se existem alterações locais:

```bash
sudo git -C /opt/migracao remote -v
sudo git -C /opt/migracao status --short
```

O destino deve ser `brunomsk9/migracaocordialpgbq`. Se houver alterações em
scripts, revise-as antes de atualizar. Para baixar a versão atual:

```bash
sudo git -C /opt/migracao pull --ff-only origin main
```

`--ff-only` interrompe a atualização se houver históricos divergentes, sem
criar um merge automaticamente. Não use `reset --hard` para resolver esse erro.
Configurações ignoradas, como `.env` e `.env.validacao`, permanecem locais.

Se o clone pertence ao seu usuário, execute os comandos Git sem `sudo`, com
esse usuário. Não é necessário configurar `safe.directory=*`.

### Caso B — /opt/migracao ainda não existe

```bash
sudo git clone https://github.com/brunomsk9/migracaocordialpgbq.git /opt/migracao
```

### Caso C — a pasta existe, mas recebeu arquivos manualmente

Se o Git responder `not a git repository`, a pasta ainda não é um clone.
Use o bloco abaixo no mesmo terminal. Ele baixa primeiro o repositório, depois
preserva a pasta antiga como backup e coloca o clone no caminho oficial:

```bash
(
  set -e
  clone_migracao=$(sudo mktemp -d /opt/migracao-clone.XXXXXX)
  sudo git clone https://github.com/brunomsk9/migracaocordialpgbq.git "$clone_migracao"
  backup_migracao="/opt/migracao-backup-$(date +%Y%m%d-%H%M%S)"
  test ! -e "$backup_migracao"
  sudo mv /opt/migracao "$backup_migracao"
  sudo mv "$clone_migracao" /opt/migracao
  for arquivo_migracao in .env .env.validacao validation-mapping.json; do
    if sudo test -f "$backup_migracao/$arquivo_migracao"; then
      sudo install -o root -g postgres -m 640 \
        "$backup_migracao/$arquivo_migracao" "/opt/migracao/$arquivo_migracao"
    fi
  done
  echo "Backup preservado em: $backup_migracao"
)
```

Anote o caminho exibido. Logs, relatórios, checkpoints, `env.txt` e outros arquivos
continuam no backup. Se sua configuração referencia um JSON de credencial ou
mapping personalizado dentro da pasta antiga, restaure esse arquivo no caminho
esperado, com acesso de leitura para `postgres`.

**Recrie o ambiente virtual no passo 3**, pois ambientes virtuais não devem ser
movidos ou copiados entre caminhos. Não apague o backup antes de conferir a instalação.

Se o GitHub pedir autenticação, use o acesso autorizado da VM. A chave SSH da
máquina de desenvolvimento não é automaticamente disponibilizada às VMs.

## 3. Preparar o Python para a validação

Se a VM já possui `/opt/migracao/.venv-new/bin/python` funcionando, pode manter
esse ambiente: substitua `.venv` por `.venv-new` nos comandos seguintes.

Verifique se o Python escolhido é **3.10 ou superior**:

```bash
python3 --version
```

Para criar um ambiente novo no Ubuntu/Debian, instale o suporte a venv:

```bash
sudo apt install -y python3-venv python3-pip
```

Em sistemas com DNF:

```bash
sudo dnf install -y python3 python3-pip
```

Se `python3` for antigo, escolha um interpretador 3.10+ disponível na distribuição
(por exemplo `python3.11`) antes de criar o ambiente. Não substitua o Python do
sistema. Crie o venv **somente se ele ainda não existir ou se veio de outra pasta**:

```bash
sudo python3 -m venv /opt/migracao/.venv
```

Instale as dependências da validação, inclusive após atualizar o código:

```bash
sudo /opt/migracao/.venv/bin/python -m pip install \
  -r /opt/migracao/requirements-validation.txt
```

Prepare a pasta de relatórios e o acesso aos scripts:

```bash
sudo chown root:postgres /opt/migracao
sudo chmod 750 /opt/migracao
sudo install -d -o postgres -g postgres -m 750 \
  /opt/migracao/validation /opt/migracao/logs
sudo -u postgres /opt/migracao/.venv/bin/python \
  /opt/migracao/validate_migration.py --help
```

Se o último comando mostrar as opções, os arquivos e o Python estão acessíveis.

## 4. Configurar somente a validação

Crie `.env.validacao` a partir do exemplo **apenas se ainda não existir**:

```bash
if ! sudo test -f /opt/migracao/.env.validacao; then
  sudo install -o root -g postgres -m 640 \
    /opt/migracao/config/examples/.env.validacao.example \
    /opt/migracao/.env.validacao
fi
sudo nano /opt/migracao/.env.validacao
```

Se `nano` não estiver instalado, use o editor disponível na VM. Ajuste:

```dotenv
PG_HOST=/var/run/postgresql
PG_PORT=5432
PG_USER=postgres
PG_PASSWORD=
PG_ADMIN_DATABASE=postgres

BQ_PROJECT=seu-projeto-gcp
BQ_LOCATION=southamerica-east1

PG_DATABASES=
PG_SCHEMAS=
PG_EXCLUDE_DATABASES=
PG_EXCLUDE_SCHEMAS=
PG_SKIP_PARTITION_CHILDREN=false
VALIDATION_COUNT_MODE=exact
VALIDATION_OUTPUT_DIR=/opt/migracao/validation
VALIDATION_BQ_TABLE=
VALIDATION_GCS_URI=
VALIDATION_MAPPING_FILE=
```

Troque `seu-projeto-gcp` pelo **Project ID** real do GCP (não o nome de exibição;
confira em `gcloud config get-value project` ou no console do GCP), a região pela
região dos datasets e `5432` pela porta real (por exemplo `5437`). Se a VM tiver
várias instâncias do PostgreSQL, execute uma rodada por porta, com arquivos de
configuração separados. Se `VALIDATION_BQ_TABLE` for preenchida mais tarde (seção
3 da [validação](validacao.md)), use o mesmo Project ID — o valor de exemplo
causa `404 Project ... is not found` do BigQuery.

Deixe `PG_DATABASES` e `PG_SCHEMAS` vazios para descobrir todos os bancos/schemas
não-sistema, respeitando as exclusões. Para restringir, use listas separadas por
vírgula. Aliases de destino precisam de um mapping: veja [validação](validacao.md).
Se houver tabelas particionadas, `PG_SKIP_PARTITION_CHILDREN=true` valida somente
a tabela pai (que já contém todas as linhas), em vez de cada partição filha também
como objeto separado.

Na VM GCP com Service Account, **remova ou comente** a linha
`GOOGLE_APPLICATION_CREDENTIALS` do arquivo, inclusive se estiver vazia. Não é
necessário copiar uma chave JSON. A conta precisa ler as tabelas do BigQuery e
executar jobs de consulta. Caso use uma chave JSON intencionalmente, configure
seu caminho absoluto e garanta que `postgres` consiga ler o arquivo.

Ajuste as permissões após salvar:

```bash
sudo chown root:postgres /opt/migracao/.env.validacao
sudo chmod 640 /opt/migracao/.env.validacao
```

## 5. Rodar somente a validação, sem publicar relatórios na nuvem

O comando abaixo força a saída local, mesmo que o arquivo de configuração tenha
um destino de relatório BigQuery ou GCS preenchido:

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  VALIDATION_BQ_TABLE= VALIDATION_GCS_URI= \
  /opt/migracao/.venv/bin/python /opt/migracao/validate_migration.py \
  --env-file /opt/migracao/.env.validacao \
  --count-mode exact \
  --fail-on-difference
codigo_validacao=$?
echo "Código de saída: $codigo_validacao"
```

Esse comando consulta o BigQuery e grava um novo `.parquet` em
`/opt/migracao/validation`. Não executa a migração e não publica o relatório.
`COUNT(*)` pode demorar e consumir recursos de consulta em bases grandes.

Para conferir **um banco, um schema e uma tabela**, substitua os três nomes de
exemplo abaixo pelos nomes reais:

```bash
sudo -u postgres env -u GOOGLE_APPLICATION_CREDENTIALS \
  VALIDATION_BQ_TABLE= VALIDATION_GCS_URI= \
  /opt/migracao/.venv/bin/python /opt/migracao/validate_migration.py \
  --env-file /opt/migracao/.env.validacao \
  --databases banco_exemplo --schemas public --tables tabela_exemplo \
  --count-mode exact --fail-on-difference
```

| Código de saída | Significado |
| --- | --- |
| `0` | Todos os objetos selecionados passaram pela validação exata |
| `2` | Há divergências ou erros individuais; consulte o relatório |
| `1` | Falha geral, como configuração, autenticação ou gravação do relatório |
| `130` | Execução interrompida pelo teclado |

Esses significados pressupõem `--count-mode exact --fail-on-difference`, como nos
comandos acima. Uma tabela `DESTINATION_NOT_FOUND` está ausente no destino esperado;
confira o mapping antes de concluir que ela precisa ser recarregada.

## 6. Abrir o relatório e localizar problemas

Liste os arquivos, começando pelos mais recentes:

```bash
sudo -u postgres sh -c 'ls -lt /opt/migracao/validation/*.parquet'
```

Para mostrar somente as divergências do relatório mais recente:

```bash
sudo -u postgres /opt/migracao/.venv/bin/python - <<'PY'
from pathlib import Path
import pandas as pd

arquivos = list(Path('/opt/migracao/validation').glob('*.parquet'))
if not arquivos:
    raise SystemExit('Nenhum relatório encontrado; confira o log da validação.')
relatorio = max(arquivos, key=lambda arquivo: arquivo.stat().st_mtime)
df = pd.read_parquet(relatorio)
colunas = ['source_database', 'source_schema', 'source_table',
           'source_rows', 'bq_rows', 'validation_status', 'error_message']
print('Relatório:', relatorio)
print(df['validation_status'].value_counts().to_string())
problemas = df.loc[~df['is_valid'], colunas]
print(problemas.to_string(index=False) if not problemas.empty else 'Nenhuma divergência.')
PY
```

Mesmo com status `OK`, o script comprova apenas contagem e estrutura, não igualdade
linha a linha. Mudanças na origem após a migração podem causar divergências.

## 7. Atualizações seguintes

Com nenhuma execução em andamento:

```bash
sudo git -C /opt/migracao pull --ff-only origin main
sudo /opt/migracao/.venv/bin/python -m pip install \
  -r /opt/migracao/requirements-validation.txt
```

Depois, repita o comando do passo 5. Não é necessário recriar `.env.validacao`.

Para rodar em background ou publicar o relatório para o Looker, siga o
[guia completo de validação](validacao.md). A publicação depende de configuração
específica; é separada da opção de relatório somente local mostrada aqui.

Referências: [instalação do Git](https://git-scm.com/book/pt-br/v2/Primeiros-Passos-Instalando-o-Git)
e [git pull](https://git-scm.com/docs/git-pull).
