# Protótipo Palm X — cadastro e verificação de palma

Prova de conceito com o **Tencent Palm X**: uma página web para celular (SDK H5 oficial) e um backend FastAPI que guarda a chave de API.

## Por que web (H5) e não app nativo

O SDK nativo do Palm X (Android, iOS, Flutter) **não é público**: a documentação manda pedir o pacote ao representante da Tencent e vincular um certificado ao `ApplicationId`/`BundleId` do app. O SDK H5 é carregado por URL pública e funciona no navegador do celular, então é o único caminho que roda só com a chave gratuita. O backend deste protótipo serve sem alteração para o app nativo depois (ver "Migração para app nativo").

## Fluxo

```
Celular (página)            Backend (FastAPI)              Palm X
   │ POST /api/token ─────────▶ │ CreateSdkUserToken (ak_) ──▶ │
   │ ◀──────── token sut_ ───── │ ◀──────────────────────────── │
   │ PalmMobileManager.start() ─────── captura, prova de vida ─▶ │
   │ ◀────────────── Result { code, data.sessionId } ─────────── │
   │ POST /api/session/confirm ▶ │ GetSessionResult (ak_) ────▶ │
   │ ◀──── confirmado: true ──── │ ◀──────────────────────────── │
```

A chave `ak_` nunca sai do servidor. O navegador só recebe um token `sut_` de 10 minutos, vinculado a um usuário. O resultado do SDK é sempre reconferido no servidor.

## Como rodar

1. **Gerar a chave**: em <https://palm.tencent.com/developer/api-keys>, "Create Application" e depois "Generate API Key". A chave aparece uma única vez.
2. **Configurar**:
   ```bash
   cp .env.example .env      # preencha PALM_API_KEY
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Testar o backend** (API da Tencent simulada, sem rede): `pytest -q`
4. **Subir**: `uvicorn backend.main:app --port 8000`
5. **Abrir no celular**. A câmera só funciona em HTTPS (`localhost` é exceção), então escolha um:
   - Túnel: `cloudflared tunnel --url http://localhost:8000` e abra a URL `https://...` no celular.
   - Cloud Run: `gcloud run deploy palm-x-prototipo --source . --region southamerica-east1 --allow-unauthenticated --set-env-vars PALM_API_KEY=ak_...`

Use o aparelho na vertical: girar para paisagem cancela a captura.

## Endpoints do backend

| Rota | Chama na Palm X | Função |
|---|---|---|
| `POST /api/token` | `CreateSdkUserToken` | Emite o token `sut_`; no cadastro exige e registra o consentimento |
| `POST /api/session/confirm` | `GetSessionResult` | Confirma cadastro ou verificação 1:1 a partir do `sessionId` |
| `GET /api/palm/{userId}` | `GetPalm` | Mostra se há palma esquerda/direita cadastrada |
| `DELETE /api/palm/{userId}` | `DeletePalm` | Apaga as palmas do usuário |

## O que foi e o que não foi testado

- **Testado**: os 10 testes do backend passam contra uma API simulada com os formatos da documentação; a página foi renderizada em tela de celular.
- **Não testado**: nenhuma chamada real à Palm X (não havia chave) e nenhuma captura com câmera. No ambiente de build o script do SDK não carregou, então o trecho que chama `PalmMobileManager.start` foi escrito pela documentação e ainda não rodou.

## Pontos em aberto na documentação

- **Criação do usuário**: a documentação diz que o usuário "é provisionado na plataforma", sem dizer se isso acontece sozinho ao emitir o token. Se o cadastro devolver o código `10021` (usuário não existe), é preciso perguntar à Tencent como criar o usuário.
- **`sessionId` no cadastro e na verificação**: a página do SDK H5 lista o campo; a do SDK nativo só o lista no reconhecimento 1:N. Se ele não vier, a página cai num plano B: confirma o cadastro via `GetPalm` e avisa que a verificação não pôde ser validada no servidor.
- **Cota e preço**: a página diz "gratuito" e 10 requisições/segundo por credencial; não informa limite de palmas.

## Limites deste protótipo

- **Sem login**: qualquer pessoa com a URL pede token para qualquer `userId`. Antes de uso real, a rota `/api/token` precisa de autenticação do seu sistema.
- **Consentimento em arquivo**: fica em `consent_log.jsonl` no disco; no Cloud Run isso se perde a cada reinício. Para algo além de teste, gravar em banco.
- **Só 1:1**: cadastro e verificação "esta palma é do usuário X?". O reconhecimento 1:N ("de quem é esta palma?") existe no SDK (`mode: 'recognition'` + `AppId`) e não foi incluído.

## LGPD

Palma é dado pessoal sensível (biométrico). O protótipo já cobre consentimento explícito antes do cadastro, registro de quando ele foi dado e exclusão a pedido. Ficam para antes de qualquer uso com pessoas reais: base legal e finalidade documentadas, aviso de privacidade, e avaliação da transferência internacional — o processamento ocorre no host internacional da Tencent.

## Migração para app nativo

Com o pacote do SDK e o certificado em mãos, o backend continua igual. No Android, o equivalente à chamada da página é:

```java
PalmMobileManager.Params params =
    new PalmMobileManager.Params.Builder(token, userId)   // token vindo de POST /api/token
        .setMode(PalmMobileManager.Mode.VERIFICATION)     // padrão: REGISTRATION
        .build();
PalmMobileManager.start(this, params, result -> { /* result.code, result.data */ });
```

Requisitos: `minSdkVersion` 24, AGP 8.5+, JDK 17; iOS 13+; Flutter 3.25+.

## Referências

- API do servidor: <https://palm.tencent.com/docs/kyc-api>
- SDK H5: <https://palm.tencent.com/docs/kyc-api/mobile-h5>
- SDK nativo: <https://palm.tencent.com/docs/kyc-api/mobile>
