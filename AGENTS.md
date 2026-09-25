# Orientações do projeto

- Este projeto é instalado nas VMs em `/opt/migracao`. Preserve os pontos de
  entrada `migrate.py` e `validate_migration.py` na raiz e mantenha os três
  módulos Python juntos nos pacotes de distribuição.
- Documentação em português em `docs/`; testes em `tests/`; configurações de
  exemplo sem segredos em `config/examples/`.
- Nunca versione `.env`, `config/local/`, credenciais, logs, checkpoints ou
  arquivos de dados. Não inclua segredos em mensagens de commit.
- Ao concluir alterações solicitadas pelo usuário, execute os testes adequados
  e faça um commit local com mensagem clara em português, conforme a preferência
  do usuário de manter as mudanças sempre commitadas. Inclua somente mudanças
  relacionadas ao trabalho concluído; não inclua alterações alheias sem revisão.
- Não faça push nem publique um repositório remoto sem solicitação do usuário.
- Para rodar a suíte completa, instale `requirements-validation.txt` e execute
  `python -m unittest discover -v` a partir da raiz.
- Gere o pacote para as VMs com `python tools/build_bundle.py`. O diretório
  `dist/` é ignorado pelo Git. Não execute migrações nas VMs como teste local.
