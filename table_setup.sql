-- Opret databasen til investeringsspillet

DROP TABLE IF EXISTS holdings;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS stocks;

CREATE TABLE users (
    user_id SERIAL PRIMARY KEY,
    name VARCHAR(30) NOT NULL,
    password_hash TEXT NOT NULL,
    cash_balance NUMERIC DEFAULT 100000
);

CREATE TABLE stocks (
    stock_id SERIAL PRIMARY KEY,
    ticker VARCHAR(25) UNIQUE NOT NULL,
    name VARCHAR(100),
    current_price NUMERIC
);

CREATE TABLE holdings (
    holding_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
    stock_id INT REFERENCES stocks(stock_id) ON DELETE CASCADE,
    quantity INT NOT NULL,
    purchase_price NUMERIC NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indsæt seed-data for 20 danske aktier
INSERT INTO stocks (ticker, name) VALUES
('NOVO-B.CO', 'Novo Nordisk B'),
('MAERSK-B.CO', 'A.P. Møller-Mærsk B'),
('DSV.CO', 'DSV A/S'),
('VWS.CO', 'Vestas Wind Systems'),
('ORSTED.CO', 'Ørsted A/S'),
('CARL-B.CO', 'Carlsberg B'),
('COLO-B.CO', 'Coloplast B'),
('GN.CO', 'GN Store Nord'),
('TRYG.CO', 'Tryg Forsikring'),
('ROCK-B.CO', 'Rockwool B'),
('LUN.CO', 'Lundbeck'),
('PNDORA.CO', 'Pandora A/S'),
('AMBU-B.CO', 'Ambu B'),
('NETC.CO', 'Netcompany Group'),
('SIM.CO', 'SimCorp'),
('ISS.CO', 'ISS A/S'),
('CHR.CO', 'Chr. Hansen Holding'),
('DEMANT.CO', 'Demant A/S'),
('TOP.CO', 'Topdanmark'),
('JYSK.CO', 'Jyske Bank');