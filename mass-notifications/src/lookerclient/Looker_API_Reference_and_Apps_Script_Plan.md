# Looker API 4.0 Reference & Apps Script Integration Plan

## Part 1: Looker API 4.0 — Complete Reference for Your Use Case

### Authentication

The Looker API uses OAuth 2.0 client credentials. Your Client ID is semi-public; your Client Secret is a password — never commit it to source control.

**Login flow:**

```
POST https://<your-instance>.cloud.looker.com/api/4.0/login
Content-Type: application/x-www-form-urlencoded

client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET
```

**Response:**

```json
{
  "access_token": "mt6Xc8jJC9GfJzKBQ5SqFZTZRVX8KY6k49TMPS8F",
  "token_type": "bearer",
  "expires_in": 3600
}
```

**Using the token:** Include in the `Authorization` header of all subsequent requests:

```
Authorization: token mt6Xc8jJC9GfJzKBQ5SqFZTZRVX8KY6k49TMPS8F
```

**Key details:**
- Tokens expire after ~1 hour (the `expires_in` field tells you exactly how long)
- A new token is generated on each `/login` call
- Revoke a token early with `DELETE /api/4.0/logout`
- All API calls must use HTTPS
- Permissions are scoped to whatever Looker role is assigned to your API user
- Always pass credentials in the POST body, not as URL query params (more secure)

---

### API Method Categories Relevant to Your Workflow

The full API has 25+ method groups. Below are the ones that matter for your dashboard-to-Sheets automation, organized by how you'll actually use them.

---

### Tier 1: Core Data Extraction (You'll use these daily)

#### Query Methods

These are the heart of getting data out of Looker programmatically.

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Run Look** | `GET` | `/api/4.0/looks/{look_id}/run/{result_format}` | Execute a saved Look and get results |
| **Run Query** | `GET` | `/api/4.0/queries/{query_id}/run/{result_format}` | Run a previously created query by ID |
| **Run Inline Query** | `POST` | `/api/4.0/queries/run/{result_format}` | Define and run a query in one call |
| **Run URL Encoded Query** | `GET` | `/api/4.0/queries/models/{model}/views/{view}/run/{result_format}` | Run a query from URL params (closest to your current manual workflow) |
| **Create Query** | `POST` | `/api/4.0/queries` | Create a reusable query object (returns query ID) |
| **Get Query** | `GET` | `/api/4.0/queries/{query_id}` | Retrieve a query's definition (model, view, fields, filters) |
| **Get Query for Slug** | `GET` | `/api/4.0/queries/slug/{slug}` | Look up a query by its short URL slug |

**`result_format` options:** `json`, `json_detail`, `csv`, `txt`, `html`, `md`, `xlsx`, `sql`, `png`, `jpg`

**Run Look — your most likely starting point:**

```
GET /api/4.0/looks/{look_id}/run/json
Authorization: token {access_token}
```

Optional query parameters:
- `limit` — Row limit (overrides saved query limit)
- `apply_formatting` — Apply model-specified formatting (boolean)
- `apply_vis` — Apply visualization options (boolean)
- `cache` — Use cached results if available (boolean, default true)

**Run Inline Query — most flexible option:**

```
POST /api/4.0/queries/run/json
Authorization: token {access_token}
Content-Type: application/json

{
  "model": "your_lookml_model",
  "view": "your_explore_view",
  "fields": [
    "view_name.field_one",
    "view_name.field_two",
    "view_name.field_three"
  ],
  "filters": {
    "view_name.property_name": "The Wayland"
  },
  "sorts": ["view_name.field_one asc"],
  "limit": "500"
}
```

**Run URL Encoded Query — closest to what you do manually in the browser:**

```
GET /api/4.0/queries/models/{model}/views/{view}/run/json
  ?fields=view.field_a,view.field_b
  &f[view.property_name]=The+Wayland
  &sorts=view.field_a+asc
  &limit=500
```

This is essentially what your browser URL is doing when you manually filter — you're already thinking in API terms.

#### Async Query Methods (for large/slow queries)

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Run Query Async** | `POST` | `/api/4.0/query_tasks` | Start a query as a background task |
| **Get Async Query Info** | `GET` | `/api/4.0/query_tasks/{task_id}` | Check task status |
| **Get Async Query Results** | `GET` | `/api/4.0/query_tasks/{task_id}/results` | Fetch results when complete |

Use these if your queries take more than 30 seconds — helps avoid timeout issues in Apps Script.

---

### Tier 2: Dashboard & Look Discovery (Useful for setup and exploration)

#### Dashboard Methods

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Get Dashboard** | `GET` | `/api/4.0/dashboards/{dashboard_id}` | Get full dashboard object including elements and filters |
| **Search Dashboards** | `GET` | `/api/4.0/dashboards/search` | Find dashboards by title, folder, etc. |
| **Get All Dashboard Elements** | `GET` | `/api/4.0/dashboards/{dashboard_id}/dashboard_elements` | List all tiles/elements on a dashboard |
| **Get All Dashboard Filters** | `GET` | `/api/4.0/dashboards/{dashboard_id}/dashboard_filters` | List all filters defined on a dashboard |
| **Search Dashboard Elements** | `GET` | `/api/4.0/dashboard_elements/search` | Find elements across dashboards |

**Why this matters for you:** When you hit that dashboard URL with `&Property+Name=The+Wayland`, the dashboard is applying that filter to one or more underlying query elements. By calling `Get Dashboard`, you can inspect:
- `dashboard_filters` — What filters exist and their default values
- `dashboard_elements` — Each tile, including its `query_id` or `look_id`
- From those `query_id` values, you can call `Run Query` directly — bypassing the dashboard entirely

**Dashboard element types:**
1. **Query tiles** — have a `query_id` directly
2. **Look-linked tiles** — reference a `look_id` (the Look itself has a `query_id`)
3. **Merge query tiles** — combine multiple source queries

#### Look Methods

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Get Look** | `GET` | `/api/4.0/looks/{look_id}` | Get Look metadata including its query_id |
| **Run Look** | `GET` | `/api/4.0/looks/{look_id}/run/{result_format}` | Execute and get results |
| **Search Looks** | `GET` | `/api/4.0/looks/search` | Find Looks by title, folder, etc. |

---

### Tier 3: Model Exploration (Helpful for building inline queries)

#### LookML Model Methods

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Get All LookML Models** | `GET` | `/api/4.0/lookml_models` | List available models |
| **Get LookML Model** | `GET` | `/api/4.0/lookml_models/{model_name}` | Get model details and its explores |
| **Get LookML Model Explore** | `GET` | `/api/4.0/lookml_models/{model_name}/explores/{explore_name}` | Get all fields, filters, joins for an explore |

**Why this matters:** If you want to build inline queries, you need to know the exact model name, view/explore name, and field names. These endpoints tell you everything available.

#### Metadata Methods

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Model Field Name Suggestions** | `GET` | `/api/4.0/models/{model_name}/views/{view_name}/fields/{field_name}/suggestions` | Get filter value suggestions for a field |

This is useful for dynamically populating property name lists.

---

### Tier 4: User & Admin (Less frequent but good to know)

| Method | HTTP | Endpoint | Purpose |
|--------|------|----------|---------|
| **Get Current User** | `GET` | `/api/4.0/user` | Verify your auth is working; see your permissions |
| **Search Users** | `GET` | `/api/4.0/users/search` | Find users (admin only) |
| **Get All Scheduled Plans** | `GET` | `/api/4.0/scheduled_plans` | List scheduled deliveries |
| **Create Scheduled Plan** | `POST` | `/api/4.0/scheduled_plans` | Schedule automated report delivery |

---

### Important API Behavior Notes

1. **Query objects are immutable** — Looker never deletes query objects. When you "create" a query, Looker first checks if an identical one already exists and returns that instead. This means query IDs are stable.

2. **Filters format** — Filters use Looker filter expression syntax, not raw SQL. Common patterns:
   - Exact match: `"The Wayland"`
   - Multiple values: `"The Wayland,The Meridian"`
   - Contains: `"%wayland%"`
   - Not: `"-The Wayland"`
   - Date range: `"2024/01/01 to 2024/12/31"`

3. **Rate limits** — Looker doesn't publish hard rate limits, but the API is subject to your instance's query concurrency limits. Don't fire 50 parallel queries.

4. **Row limits** — Default is usually 5000. Set `limit=-1` for unlimited results (be careful with large datasets in Apps Script due to memory).

5. **Caching** — By default, API queries use Looker's cache. Set `cache=false` to force fresh data.

---

## Part 2: Apps Script Architecture — Property Dashboard Automation

### Overview

This script automates your manual workflow: querying a Looker dashboard filtered by property name, extracting the data, deduplicating, validating, and composing columns — all landing in a Google Sheet.

### Architecture

```
┌─────────────────────────────────────────────────┐
│  Google Sheets (Destination)                     │
│  ┌─────────────┐  ┌─────────────┐               │
│  │ Config Sheet │  │ Output Sheet│               │
│  │ - API creds  │  │ - Clean data│               │
│  │ - Properties │  │ - Composed  │               │
│  │ - Field map  │  │   columns   │               │
│  └──────┬──────┘  └──────▲──────┘               │
│         │                │                       │
└─────────┼────────────────┼───────────────────────┘
          │                │
          ▼                │
┌─────────────────────────────────────────────────┐
│  Apps Script                                     │
│                                                  │
│  1. LookerAuth module                            │
│     └─ login() → access_token                    │
│     └─ getToken() → cached or refreshed token    │
│                                                  │
│  2. LookerQuery module                           │
│     └─ runLook(lookId, filters) → JSON           │
│     └─ runInlineQuery(model, view, fields,       │
│        filters) → JSON                           │
│     └─ getDashboardElements(dashId) → element[]  │
│                                                  │
│  3. DataProcessor module                         │
│     └─ deduplicate(data, keyFields) → clean[]    │
│     └─ validateRows(data, rules) → valid[]       │
│     └─ composeColumns(data, config) → final[]    │
│                                                  │
│  4. SheetWriter module                           │
│     └─ writeToSheet(sheetName, data)             │
│     └─ clearAndWrite(sheetName, data)            │
│                                                  │
│  5. Main orchestrator                            │
│     └─ pullPropertyData(propertyName)            │
│     └─ pullAllProperties()                       │
│     └─ onOpen() → custom menu                    │
│                                                  │
└─────────────────────────────────────────────────┘
```

### File Structure (Apps Script project)

```
├── Config.gs          — Constants, API URL, credential retrieval
├── LookerAuth.gs      — Authentication and token management
├── LookerQuery.gs     — API call wrappers for query/look/dashboard
├── DataProcessor.gs   — Dedup, validation, column composition
├── SheetWriter.gs     — Google Sheets output functions
├── Main.gs            — Orchestration, menu, triggers
```

### Module 1: Config.gs

```javascript
/**
 * Retrieves Looker configuration from a "Config" sheet
 * or Script Properties (more secure for credentials).
 *
 * SECURITY NOTE: Store Client ID and Client Secret in
 * Script Properties (File > Project properties > Script properties)
 * NOT in a sheet cell where other editors can see them.
 */

function getLookerConfig() {
  const props = PropertiesService.getScriptProperties();
  return {
    baseUrl: props.getProperty('LOOKER_BASE_URL'),
    // e.g., "https://yourcompany.cloud.looker.com"
    clientId: props.getProperty('LOOKER_CLIENT_ID'),
    clientSecret: props.getProperty('LOOKER_CLIENT_SECRET'),
    apiVersion: '4.0'
  };
}

/**
 * One-time setup function — run this manually to store credentials.
 * After running, delete or comment out the secret values.
 */
function setupCredentials() {
  const props = PropertiesService.getScriptProperties();
  props.setProperties({
    'LOOKER_BASE_URL': 'https://yourcompany.cloud.looker.com',
    'LOOKER_CLIENT_ID': 'YOUR_CLIENT_ID_HERE',
    'LOOKER_CLIENT_SECRET': 'YOUR_CLIENT_SECRET_HERE'
  });
  Logger.log('Credentials stored in Script Properties.');
}
```

### Module 2: LookerAuth.gs

```javascript
/**
 * Authenticates with Looker API and returns an access token.
 * Caches the token in CacheService to avoid re-authenticating
 * on every call within the same execution or within 50 minutes.
 */

function getLookerAccessToken() {
  const cache = CacheService.getScriptCache();
  const cached = cache.get('looker_access_token');
  if (cached) return cached;

  const config = getLookerConfig();
  const loginUrl = config.baseUrl + '/api/' +
                   config.apiVersion + '/login';

  const response = UrlFetchApp.fetch(loginUrl, {
    method: 'post',
    contentType: 'application/x-www-form-urlencoded',
    payload: {
      client_id: config.clientId,
      client_secret: config.clientSecret
    },
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error('Looker login failed: ' +
                    response.getContentText());
  }

  const result = JSON.parse(response.getContentText());
  // Cache for 50 minutes (token lasts 60, buffer of 10)
  cache.put('looker_access_token',
            result.access_token, 50 * 60);
  return result.access_token;
}

/**
 * Helper: builds the Authorization header object.
 */
function lookerAuthHeaders() {
  return {
    'Authorization': 'token ' + getLookerAccessToken()
  };
}
```

### Module 3: LookerQuery.gs

```javascript
/**
 * Runs a saved Look and returns parsed JSON results.
 *
 * @param {number} lookId — The Look ID (from its URL)
 * @param {Object} [filters] — Optional filter overrides
 *   e.g., { "view.property_name": "The Wayland" }
 * @param {number} [limit] — Row limit (default: no override)
 * @returns {Object[]} Array of result row objects
 */
function runLook(lookId, filters, limit) {
  const config = getLookerConfig();
  let url = config.baseUrl + '/api/' + config.apiVersion +
            '/looks/' + lookId + '/run/json';

  const params = [];
  if (limit) params.push('limit=' + limit);
  if (params.length) url += '?' + params.join('&');

  const response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: lookerAuthHeaders(),
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error('Run Look failed (' + lookId + '): ' +
                    response.getContentText());
  }
  return JSON.parse(response.getContentText());
}

/**
 * Runs an inline query against a LookML model/explore.
 * This is the most flexible approach — you define the
 * exact fields and filters programmatically.
 *
 * @param {Object} queryDef — Query definition object
 * @param {string} queryDef.model — LookML model name
 * @param {string} queryDef.view — Explore/view name
 * @param {string[]} queryDef.fields — Array of field names
 * @param {Object} queryDef.filters — Filter key-value pairs
 * @param {string[]} [queryDef.sorts] — Sort expressions
 * @param {number} [queryDef.limit] — Row limit
 * @returns {Object[]} Array of result row objects
 */
function runInlineQuery(queryDef) {
  const config = getLookerConfig();
  const url = config.baseUrl + '/api/' + config.apiVersion +
              '/queries/run/json';

  const body = {
    model: queryDef.model,
    view: queryDef.view,
    fields: queryDef.fields,
    filters: queryDef.filters || {},
    sorts: queryDef.sorts || [],
    limit: String(queryDef.limit || 500)
  };

  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    headers: lookerAuthHeaders(),
    contentType: 'application/json',
    payload: JSON.stringify(body),
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error('Inline query failed: ' +
                    response.getContentText());
  }
  return JSON.parse(response.getContentText());
}

/**
 * Gets dashboard metadata including all elements and filters.
 * Use this to discover the query_ids behind dashboard tiles.
 *
 * @param {string} dashboardId — Dashboard ID (from its URL)
 * @returns {Object} Full dashboard object
 */
function getDashboard(dashboardId) {
  const config = getLookerConfig();
  const url = config.baseUrl + '/api/' + config.apiVersion +
              '/dashboards/' + dashboardId;

  const response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: lookerAuthHeaders(),
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error('Get dashboard failed: ' +
                    response.getContentText());
  }
  return JSON.parse(response.getContentText());
}

/**
 * Runs a query by its ID (obtained from dashboard element
 * or Look metadata).
 *
 * @param {number} queryId — Numeric query ID
 * @param {string} [format] — Result format (default 'json')
 * @returns {Object[]|string} Results in requested format
 */
function runQueryById(queryId, format) {
  format = format || 'json';
  const config = getLookerConfig();
  const url = config.baseUrl + '/api/' + config.apiVersion +
              '/queries/' + queryId + '/run/' + format;

  const response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: lookerAuthHeaders(),
    muteHttpExceptions: true
  });

  if (response.getResponseCode() !== 200) {
    throw new Error('Run query failed (' + queryId + '): ' +
                    response.getContentText());
  }

  if (format === 'json') {
    return JSON.parse(response.getContentText());
  }
  return response.getContentText();
}

/**
 * Discovery helper: lists all explores and fields in a model.
 * Run this once to understand what field names to use.
 *
 * @param {string} modelName — LookML model name
 * @returns {Object} Model details with explores
 */
function exploreModel(modelName) {
  const config = getLookerConfig();
  const url = config.baseUrl + '/api/' + config.apiVersion +
              '/lookml_models/' + modelName;

  const response = UrlFetchApp.fetch(url, {
    method: 'get',
    headers: lookerAuthHeaders(),
    muteHttpExceptions: true
  });

  return JSON.parse(response.getContentText());
}
```

### Module 4: DataProcessor.gs

```javascript
/**
 * Deduplicates rows based on one or more key fields.
 * Keeps the first occurrence of each unique key combination.
 *
 * @param {Object[]} data — Array of row objects
 * @param {string[]} keyFields — Field names that define uniqueness
 * @returns {Object[]} Deduplicated array
 */
function deduplicateRows(data, keyFields) {
  const seen = new Set();
  return data.filter(function(row) {
    const key = keyFields.map(function(f) {
      return String(row[f] || '').trim().toLowerCase();
    }).join('|||');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * Validates rows against a set of rules.
 * Returns only rows that pass all validations.
 *
 * @param {Object[]} data — Array of row objects
 * @param {Object} rules — Validation rules
 * @param {string[]} [rules.requiredFields] — Fields that must be non-empty
 * @param {Object} [rules.patterns] — Field: regex pattern pairs
 * @param {Function} [rules.customValidator] — fn(row) => boolean
 * @returns {Object} { valid: [], invalid: [], stats: {} }
 */
function validateRows(data, rules) {
  var valid = [];
  var invalid = [];

  data.forEach(function(row) {
    var isValid = true;
    var reason = '';

    // Check required fields
    if (rules.requiredFields) {
      for (var i = 0; i < rules.requiredFields.length; i++) {
        var field = rules.requiredFields[i];
        if (!row[field] || String(row[field]).trim() === '') {
          isValid = false;
          reason = 'Missing required field: ' + field;
          break;
        }
      }
    }

    // Check regex patterns
    if (isValid && rules.patterns) {
      var patternFields = Object.keys(rules.patterns);
      for (var j = 0; j < patternFields.length; j++) {
        var pField = patternFields[j];
        var pattern = new RegExp(rules.patterns[pField]);
        if (row[pField] && !pattern.test(String(row[pField]))) {
          isValid = false;
          reason = 'Pattern mismatch on: ' + pField;
          break;
        }
      }
    }

    // Custom validator
    if (isValid && rules.customValidator) {
      isValid = rules.customValidator(row);
      if (!isValid) reason = 'Custom validation failed';
    }

    if (isValid) {
      valid.push(row);
    } else {
      row._invalidReason = reason;
      invalid.push(row);
    }
  });

  return {
    valid: valid,
    invalid: invalid,
    stats: {
      total: data.length,
      validCount: valid.length,
      invalidCount: invalid.length
    }
  };
}

/**
 * Composes new columns by combining existing fields.
 *
 * Example config:
 * [
 *   {
 *     newField: "full_address",
 *     template: "{street}, {city}, {state} {zip}",
 *     sourceFields: ["street", "city", "state", "zip"]
 *   },
 *   {
 *     newField: "display_name",
 *     composeFn: function(row) {
 *       return row["last_name"] + ", " + row["first_name"];
 *     }
 *   }
 * ]
 *
 * @param {Object[]} data — Array of row objects
 * @param {Object[]} compositions — Composition configs
 * @returns {Object[]} Data with new composed columns added
 */
function composeColumns(data, compositions) {
  return data.map(function(row) {
    var newRow = Object.assign({}, row);

    compositions.forEach(function(comp) {
      if (comp.composeFn) {
        newRow[comp.newField] = comp.composeFn(row);
      } else if (comp.template) {
        var result = comp.template;
        comp.sourceFields.forEach(function(field) {
          result = result.replace(
            '{' + field + '}',
            String(row[field] || '').trim()
          );
        });
        newRow[comp.newField] = result;
      }
    });

    return newRow;
  });
}
```

### Module 5: SheetWriter.gs

```javascript
/**
 * Writes an array of objects to a named sheet.
 * Creates headers from object keys.
 *
 * @param {string} sheetName — Target sheet tab name
 * @param {Object[]} data — Array of row objects
 * @param {string[]} [columnOrder] — Optional column ordering
 * @param {boolean} [append] — Append instead of overwrite
 */
function writeToSheet(sheetName, data, columnOrder, append) {
  if (!data || data.length === 0) {
    Logger.log('No data to write to ' + sheetName);
    return;
  }

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(sheetName);
  if (!sheet) {
    sheet = ss.insertSheet(sheetName);
  }

  // Determine column order
  var headers = columnOrder ||
                Object.keys(data[0]);

  // Build 2D array
  var rows = data.map(function(row) {
    return headers.map(function(h) {
      return row[h] !== undefined ? row[h] : '';
    });
  });

  if (append && sheet.getLastRow() > 0) {
    // Append below existing data
    sheet.getRange(
      sheet.getLastRow() + 1, 1,
      rows.length, headers.length
    ).setValues(rows);
  } else {
    // Clear and write with headers
    sheet.clearContents();
    sheet.getRange(1, 1, 1, headers.length)
         .setValues([headers])
         .setFontWeight('bold');
    if (rows.length > 0) {
      sheet.getRange(2, 1, rows.length, headers.length)
           .setValues(rows);
    }
  }

  Logger.log('Wrote ' + rows.length + ' rows to ' + sheetName);
}
```

### Module 6: Main.gs — Orchestrator

```javascript
/**
 * Adds a custom menu to the spreadsheet UI.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Looker Sync')
    .addItem('Pull Property Data...', 'showPropertyPrompt')
    .addItem('Pull All Properties', 'pullAllProperties')
    .addSeparator()
    .addItem('Explore Dashboard Structure', 'exploreDashboard')
    .addItem('Test Connection', 'testConnection')
    .addToUi();
}

/**
 * Quick connection test — verifies credentials work.
 */
function testConnection() {
  try {
    var token = getLookerAccessToken();
    var config = getLookerConfig();
    var url = config.baseUrl + '/api/' +
              config.apiVersion + '/user';
    var response = UrlFetchApp.fetch(url, {
      headers: { 'Authorization': 'token ' + token },
      muteHttpExceptions: true
    });
    var user = JSON.parse(response.getContentText());
    SpreadsheetApp.getUi().alert(
      'Connected as: ' + user.display_name +
      '\nEmail: ' + user.email
    );
  } catch (e) {
    SpreadsheetApp.getUi().alert('Connection failed: ' + e.message);
  }
}

/**
 * Prompts user for a property name, then pulls data.
 */
function showPropertyPrompt() {
  var ui = SpreadsheetApp.getUi();
  var result = ui.prompt(
    'Pull Property Data',
    'Enter property name (e.g., The Wayland):',
    ui.ButtonSet.OK_CANCEL
  );
  if (result.getSelectedButton() === ui.Button.OK) {
    pullPropertyData(result.getResponseText().trim());
  }
}

/**
 * Main pipeline: pulls data for a single property.
 *
 * CUSTOMIZE THIS FUNCTION for your specific:
 * - Look ID or inline query definition
 * - Dedup key fields
 * - Validation rules
 * - Column compositions
 *
 * @param {string} propertyName — e.g., "The Wayland"
 */
function pullPropertyData(propertyName) {
  Logger.log('Pulling data for: ' + propertyName);

  // ── Step 1: Query Looker ──────────────────────────
  // OPTION A: Run a saved Look with filter override
  // (Replace LOOK_ID with your actual Look ID)
  //
  // var rawData = runLook(LOOK_ID);
  //
  // OPTION B: Run an inline query (more flexible)
  var rawData = runInlineQuery({
    model: 'your_model_name',         // ← CUSTOMIZE
    view: 'your_explore_name',        // ← CUSTOMIZE
    fields: [
      'view.property_name',           // ← CUSTOMIZE
      'view.resident_name',
      'view.unit_number',
      'view.email',
      'view.phone',
      'view.move_in_date',
      'view.status'
    ],
    filters: {
      'view.property_name': propertyName  // ← dynamic filter
    },
    limit: 5000
  });

  Logger.log('Raw rows: ' + rawData.length);

  // ── Step 2: Deduplicate ───────────────────────────
  var deduped = deduplicateRows(rawData, [
    'view.resident_name',             // ← CUSTOMIZE key fields
    'view.unit_number'
  ]);
  Logger.log('After dedup: ' + deduped.length);

  // ── Step 3: Validate ──────────────────────────────
  var validated = validateRows(deduped, {
    requiredFields: [
      'view.resident_name',           // ← CUSTOMIZE
      'view.email'
    ],
    patterns: {
      'view.email': '^.+@.+\\..+$'   // basic email check
    }
  });
  Logger.log('Valid: ' + validated.stats.validCount +
             ', Invalid: ' + validated.stats.invalidCount);

  // ── Step 4: Compose columns ───────────────────────
  var final = composeColumns(validated.valid, [
    {
      newField: 'contact_line',       // ← CUSTOMIZE
      composeFn: function(row) {
        var name = row['view.resident_name'] || '';
        var unit = row['view.unit_number'] || '';
        var email = row['view.email'] || '';
        return name + ' (Unit ' + unit + ') — ' + email;
      }
    }
  ]);

  // ── Step 5: Write to Sheet ────────────────────────
  writeToSheet(propertyName, final, [
    'view.property_name',
    'view.resident_name',
    'view.unit_number',
    'view.email',
    'view.phone',
    'contact_line',
    'view.status'
  ]);

  // Write invalids to a separate tab for review
  if (validated.invalid.length > 0) {
    writeToSheet(propertyName + ' — INVALID',
                 validated.invalid);
  }

  SpreadsheetApp.getUi().alert(
    'Done! ' + final.length + ' valid rows written to "' +
    propertyName + '".\n' +
    validated.stats.invalidCount + ' invalid rows logged.'
  );
}

/**
 * Pulls data for all properties listed in a Config sheet.
 * Expects a "Properties" sheet with property names in column A.
 */
function pullAllProperties() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet()
                            .getSheetByName('Properties');
  if (!sheet) {
    SpreadsheetApp.getUi().alert(
      'Create a "Properties" sheet with names in column A.'
    );
    return;
  }

  var names = sheet.getRange('A2:A' + sheet.getLastRow())
                   .getValues()
                   .flat()
                   .filter(function(n) { return n; });

  names.forEach(function(name) {
    try {
      pullPropertyData(String(name).trim());
    } catch (e) {
      Logger.log('Error on "' + name + '": ' + e.message);
    }
  });

  SpreadsheetApp.getUi().alert(
    'Finished pulling ' + names.length + ' properties.'
  );
}

/**
 * Discovery tool: shows the structure of a dashboard
 * so you can find Look IDs and query IDs.
 */
function exploreDashboard() {
  var ui = SpreadsheetApp.getUi();
  var result = ui.prompt(
    'Explore Dashboard',
    'Enter dashboard ID (from the URL):',
    ui.ButtonSet.OK_CANCEL
  );
  if (result.getSelectedButton() !== ui.Button.OK) return;

  var dash = getDashboard(result.getResponseText().trim());

  var info = 'Dashboard: ' + dash.title + '\n\n';

  info += '— FILTERS —\n';
  (dash.dashboard_filters || []).forEach(function(f) {
    info += '  ' + f.title + ' (' + f.name + ')\n';
    info += '    Field: ' + f.dimension + '\n';
    info += '    Default: ' + (f.default_value || 'none') + '\n';
  });

  info += '\n— ELEMENTS (TILES) —\n';
  (dash.dashboard_elements || []).forEach(function(el) {
    info += '  [' + el.id + '] ' + (el.title || '(untitled)') + '\n';
    if (el.look_id) info += '    Look ID: ' + el.look_id + '\n';
    if (el.query_id) info += '    Query ID: ' + el.query_id + '\n';
    if (el.query) {
      info += '    Model: ' + el.query.model + '\n';
      info += '    View: ' + el.query.view + '\n';
    }
  });

  Logger.log(info);
  ui.alert(info.substring(0, 2000)); // UI alert has char limit
}
```

---

## Part 3: Implementation Roadmap

### Phase 1 — Get Connected (Day 1)

1. Create a new Google Sheet
2. Open Apps Script (Extensions > Apps Script)
3. Create `Config.gs` and `LookerAuth.gs`
4. Run `setupCredentials()` to store your Client ID / Secret
5. Run `testConnection()` to verify it works

### Phase 2 — Discover Your Data (Day 1-2)

1. Create `LookerQuery.gs`
2. Run `exploreDashboard()` with your dashboard ID
3. Note the Look IDs and query IDs for each tile
4. Run `exploreModel()` to see exact field names
5. Try `runLook()` or `runQueryById()` to see raw data shapes

### Phase 3 — Build the Pipeline (Day 2-3)

1. Create `DataProcessor.gs` and `SheetWriter.gs`
2. Customize `pullPropertyData()` in `Main.gs`:
   - Replace placeholder model/view/field names with real ones
   - Define your dedup key fields
   - Set up your validation rules
   - Configure your column compositions
3. Test with a single property name

### Phase 4 — Scale and Automate (Day 3-4)

1. Create the "Properties" sheet with all property names
2. Test `pullAllProperties()`
3. Set up a time-driven trigger for scheduled runs
4. Add error logging to a "Logs" sheet

### Phase 5 — Harden (Week 2)

1. Add retry logic for transient API failures
2. Add execution time monitoring (Apps Script 6-min limit)
3. Consider splitting large property lists across multiple trigger runs
4. Add a "Last Synced" timestamp per property

---

## Appendix: Quick Reference Card

| What you want to do | API method | Endpoint |
|---------------------|-----------|----------|
| Authenticate | Login | `POST /api/4.0/login` |
| Run a saved Look | Run Look | `GET /api/4.0/looks/{id}/run/json` |
| Run a custom query | Run Inline Query | `POST /api/4.0/queries/run/json` |
| Run a query by ID | Run Query | `GET /api/4.0/queries/{id}/run/json` |
| See dashboard structure | Get Dashboard | `GET /api/4.0/dashboards/{id}` |
| List dashboard tiles | Get Dashboard Elements | `GET /api/4.0/dashboards/{id}/dashboard_elements` |
| Discover field names | Get Explore | `GET /api/4.0/lookml_models/{model}/explores/{explore}` |
| Check who you're authed as | Get Current User | `GET /api/4.0/user` |
| Cancel a running query | Kill Running Query | `DELETE /api/4.0/running_queries/{id}` |

**API docs home:** https://docs.cloud.google.com/looker/docs/reference/looker-api/latest

**Apps Script UrlFetchApp docs:** https://developers.google.com/apps-script/reference/url-fetch/url-fetch-app
