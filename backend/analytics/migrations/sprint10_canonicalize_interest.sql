-- ============================================================
-- Sprint 10 — canonicalize_interest()  [comprehensive map]
-- ============================================================
-- A PostgreSQL function that normalises research-interest labels
-- into a canonical form so synonyms / abbreviations / typos /
-- spelling variants compare equal.
--
-- WHY THIS EXISTS
--   Researchers write the same concept many different ways:
--       AI, A.I., Artifical Intelligance, ccArtificial Intelligence
--       ML, M.L, machine-learning, MachineLearning
--       IoT, I.O.T, Internet of Things, IOT
--       SE, S.E., Software Engineer, software-engineering
--   A naive LOWER() match misses all of these. This function
--   collapses them into one canonical token so the "shared
--   interests" computation in network_views.py becomes accurate.
--
-- DESIGN
--   * IMMUTABLE: PostgreSQL can index it later (expression index).
--   * STRICT: NULL in → NULL out (cuts NULL checks at call sites).
--   * Raw label is NEVER mutated. We always store what the
--     researcher wrote (preserve Scholar profile UX). The function
--     is invoked only at COMPARISON time.
--   * The map is grouped by topic family + alphabetical within
--     each — keeps review tractable.
--
-- TO EXTEND
--   Add WHEN clauses below, re-apply the migration. PostgreSQL
--   replaces the function in place; no data migration needed.
-- ============================================================

CREATE OR REPLACE FUNCTION canonicalize_interest(raw text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE STRICT
AS $$
DECLARE
    norm text;
BEGIN
    -- ---- Lexical normalisation ----
    norm := LOWER(TRIM(raw));

    -- Strip bullet/symbol pollution at the ends (• - * · + and whitespace)
    norm := REGEXP_REPLACE(norm, '^[•\-*·+\s]+', '', 'g');
    norm := REGEXP_REPLACE(norm, '[•\-*·+\s]+$', '', 'g');

    -- Strip stray Scholar prefixes / qualifiers
    norm := REGEXP_REPLACE(norm, '\s*\(.*?\)\s*', ' ', 'g');   -- "Foo (Bar)" → "Foo "
    norm := REGEXP_REPLACE(norm, '\s*&\s*', ' and ', 'g');     -- "AI & ML" → "ai and ml"

    -- Replace separators with spaces (handles "machine-learning", "a.i",
    -- "data_science", "deep/learning", "human–computer")
    norm := REGEXP_REPLACE(norm, '[\.\-_/\\–—]+', ' ', 'g');

    -- Collapse multiple spaces
    norm := REGEXP_REPLACE(norm, '\s+', ' ', 'g');
    norm := TRIM(norm);

    -- ---- Synonym map ----
    -- Grouped by family. ADD ENTRIES HERE when you observe a
    -- real-world duplicate causing a missed match.
    RETURN CASE norm

        -- ============================================================
        -- ARTIFICIAL INTELLIGENCE
        -- ============================================================
        WHEN 'ai'                              THEN 'artificial intelligence'
        WHEN 'a i'                             THEN 'artificial intelligence'
        WHEN 'artifical intelligence'          THEN 'artificial intelligence'
        WHEN 'artifical intelligance'          THEN 'artificial intelligence'
        WHEN 'artificial intelligance'         THEN 'artificial intelligence'
        WHEN 'artificialintelligence'          THEN 'artificial intelligence'
        WHEN 'ccartificial intelligence'       THEN 'artificial intelligence'
        WHEN 'cc artificial intelligence'      THEN 'artificial intelligence'
        WHEN 'applied artificial intelligence' THEN 'artificial intelligence'
        WHEN 'artificial inteligence'          THEN 'artificial intelligence'

        -- Sub-fields of AI (kept distinct from "AI" itself)
        WHEN 'ml'                              THEN 'machine learning'
        WHEN 'm l'                             THEN 'machine learning'
        WHEN 'machinelearning'                 THEN 'machine learning'
        WHEN 'machine learning algorithms'     THEN 'machine learning'
        WHEN 'applied machine learning'        THEN 'machine learning'
        WHEN 'supervised learning'             THEN 'machine learning'
        WHEN 'unsupervised learning'           THEN 'machine learning'

        WHEN 'dl'                              THEN 'deep learning'
        WHEN 'd l'                             THEN 'deep learning'
        WHEN 'deeplearning'                    THEN 'deep learning'
        WHEN 'deep neural network'             THEN 'deep learning'
        WHEN 'deep neural networks'            THEN 'deep learning'
        WHEN 'dnn'                             THEN 'deep learning'

        WHEN 'rl'                              THEN 'reinforcement learning'
        WHEN 'r l'                             THEN 'reinforcement learning'

        -- Natural Language Processing (broad family)
        WHEN 'nlp'                             THEN 'natural language processing'
        WHEN 'n l p'                           THEN 'natural language processing'
        WHEN 'n.l.p'                           THEN 'natural language processing'
        WHEN 'natural language proc'           THEN 'natural language processing'
        WHEN 'natural language processing nlp' THEN 'natural language processing'
        WHEN 'language technology'             THEN 'natural language processing'
        WHEN 'language technologies'           THEN 'natural language processing'
        WHEN 'computational linguistics'       THEN 'natural language processing'
        WHEN 'corpus linguistics'              THEN 'natural language processing'
        WHEN 'corpus based linguistics'        THEN 'natural language processing'
        WHEN 'arabic nlp'                      THEN 'natural language processing'
        WHEN 'arabic natural language processing' THEN 'natural language processing'
        WHEN 'multilingual nlp'                THEN 'natural language processing'
        WHEN 'cross lingual nlp'               THEN 'natural language processing'

        -- Text mining / analytics
        WHEN 'text mining'                     THEN 'text mining'
        WHEN 'text analytics'                  THEN 'text mining'
        WHEN 'text analysis'                   THEN 'text mining'
        WHEN 'document mining'                 THEN 'text mining'
        WHEN 'web mining'                      THEN 'text mining'
        WHEN 'opinion mining'                  THEN 'sentiment analysis'

        -- Text classification / sentiment
        WHEN 'text classification'             THEN 'text classification'
        WHEN 'document classification'         THEN 'text classification'
        WHEN 'document categorization'         THEN 'text classification'
        WHEN 'spam detection'                  THEN 'text classification'

        WHEN 'sentiment analysis'              THEN 'sentiment analysis'
        WHEN 'sentiment classification'        THEN 'sentiment analysis'
        WHEN 'sa'                              THEN 'sentiment analysis'
        WHEN 'aspect based sentiment'          THEN 'sentiment analysis'
        WHEN 'absa'                            THEN 'sentiment analysis'

        -- Information extraction / NER
        WHEN 'information extraction'          THEN 'information extraction'
        WHEN 'ie'                              THEN 'information extraction'
        WHEN 'ner'                             THEN 'named entity recognition'
        WHEN 'named entity recognition'        THEN 'named entity recognition'
        WHEN 'entity recognition'              THEN 'named entity recognition'
        WHEN 'entity extraction'               THEN 'named entity recognition'
        WHEN 'entity linking'                  THEN 'entity linking'

        -- Summarization / generation
        WHEN 'text summarization'              THEN 'text summarisation'
        WHEN 'text summarisation'              THEN 'text summarisation'
        WHEN 'document summarization'          THEN 'text summarisation'
        WHEN 'automatic summarization'         THEN 'text summarisation'

        WHEN 'text generation'                 THEN 'text generation'
        WHEN 'natural language generation'     THEN 'text generation'
        WHEN 'nlg'                             THEN 'text generation'
        WHEN 'language generation'             THEN 'text generation'

        -- Topic modeling
        WHEN 'topic modeling'                  THEN 'topic modeling'
        WHEN 'topic modelling'                 THEN 'topic modeling'
        WHEN 'latent dirichlet allocation'     THEN 'topic modeling'
        WHEN 'lda'                             THEN 'topic modeling'

        -- Embeddings & representation
        WHEN 'word embeddings'                 THEN 'word embeddings'
        WHEN 'word embedding'                  THEN 'word embeddings'
        WHEN 'word2vec'                        THEN 'word embeddings'
        WHEN 'glove'                           THEN 'word embeddings'
        WHEN 'fasttext'                        THEN 'word embeddings'
        WHEN 'sentence embeddings'             THEN 'word embeddings'
        WHEN 'embedding'                       THEN 'word embeddings'
        WHEN 'embeddings'                      THEN 'word embeddings'

        -- Language models
        WHEN 'language model'                  THEN 'language models'
        WHEN 'language models'                 THEN 'language models'
        WHEN 'pretrained language models'      THEN 'language models'
        WHEN 'bert'                            THEN 'language models'
        WHEN 'gpt'                             THEN 'language models'
        WHEN 'gpt 2'                           THEN 'language models'
        WHEN 'gpt 3'                           THEN 'language models'
        WHEN 'gpt 4'                           THEN 'language models'
        WHEN 'arabert'                         THEN 'language models'
        WHEN 'camembert'                       THEN 'language models'
        WHEN 'xlm'                             THEN 'language models'
        WHEN 'xlm r'                           THEN 'language models'

        -- Machine translation
        WHEN 'machine translation'             THEN 'machine translation'
        WHEN 'mt'                              THEN 'machine translation'
        WHEN 'neural machine translation'      THEN 'machine translation'
        WHEN 'nmt'                             THEN 'machine translation'
        WHEN 'translation'                     THEN 'machine translation'
        WHEN 'automatic translation'           THEN 'machine translation'

        -- Question answering / chatbots
        WHEN 'question answering'              THEN 'question answering'
        WHEN 'qa system'                       THEN 'question answering'
        WHEN 'q a'                             THEN 'question answering'
        WHEN 'chatbot'                         THEN 'conversational ai'
        WHEN 'chatbots'                        THEN 'conversational ai'
        WHEN 'dialogue system'                 THEN 'conversational ai'
        WHEN 'dialogue systems'                THEN 'conversational ai'
        WHEN 'conversational ai'               THEN 'conversational ai'
        WHEN 'conversational agents'           THEN 'conversational ai'

        -- POS / parsing / morphology
        WHEN 'pos tagging'                     THEN 'pos tagging'
        WHEN 'part of speech tagging'          THEN 'pos tagging'
        WHEN 'syntactic parsing'               THEN 'parsing'
        WHEN 'dependency parsing'              THEN 'parsing'
        WHEN 'parser'                          THEN 'parsing'
        WHEN 'morphological analysis'          THEN 'morphological analysis'
        WHEN 'morphology'                      THEN 'morphological analysis'
        WHEN 'arabic morphology'               THEN 'morphological analysis'
        WHEN 'lemmatization'                   THEN 'morphological analysis'
        WHEN 'stemming'                        THEN 'morphological analysis'
        WHEN 'tokenization'                    THEN 'morphological analysis'

        -- Speech (NLP-adjacent)
        WHEN 'speech recognition'              THEN 'speech processing'
        WHEN 'speech processing'               THEN 'speech processing'
        WHEN 'automatic speech recognition'    THEN 'speech processing'
        WHEN 'asr'                             THEN 'speech processing'
        WHEN 'speech to text'                  THEN 'speech processing'
        WHEN 'stt'                             THEN 'speech processing'
        WHEN 'text to speech'                  THEN 'speech processing'
        WHEN 'tts'                             THEN 'speech processing'
        WHEN 'speech synthesis'                THEN 'speech processing'

        WHEN 'cv'                              THEN 'computer vision'
        WHEN 'c v'                             THEN 'computer vision'
        WHEN 'computervision'                  THEN 'computer vision'
        WHEN 'machine vision'                  THEN 'computer vision'
        WHEN 'image recognition'               THEN 'computer vision'
        WHEN 'object detection'                THEN 'computer vision'

        WHEN 'ann'                             THEN 'neural networks'
        WHEN 'a n n'                           THEN 'neural networks'
        WHEN 'neural network'                  THEN 'neural networks'
        WHEN 'artificial neural network'       THEN 'neural networks'
        WHEN 'artificial neural networks'      THEN 'neural networks'

        WHEN 'genetic algorithm'               THEN 'evolutionary computing'
        WHEN 'genetic algorithms'              THEN 'evolutionary computing'
        WHEN 'evolutionary algorithms'         THEN 'evolutionary computing'
        WHEN 'evolutionary computation'        THEN 'evolutionary computing'

        WHEN 'fuzzy logic'                     THEN 'fuzzy systems'
        WHEN 'fuzzy sets'                      THEN 'fuzzy systems'
        WHEN 'fuzzy set'                       THEN 'fuzzy systems'

        WHEN 'ci'                              THEN 'computational intelligence'
        WHEN 'c i'                             THEN 'computational intelligence'

        WHEN 'ambient'                         THEN 'ambient intelligence'
        WHEN 'ami'                             THEN 'ambient intelligence'

        WHEN 'agent'                           THEN 'multi agent systems'
        WHEN 'agents'                          THEN 'multi agent systems'
        WHEN 'mas'                             THEN 'multi agent systems'
        WHEN 'multi agent'                     THEN 'multi agent systems'
        WHEN 'multiagent'                      THEN 'multi agent systems'
        WHEN 'multiagent systems'              THEN 'multi agent systems'
        WHEN 'multi agent system'              THEN 'multi agent systems'
        WHEN 'agent and multi agent'           THEN 'multi agent systems'
        WHEN 'agent based modeling'            THEN 'multi agent systems'

        WHEN 'expert system'                   THEN 'expert systems'
        WHEN 'knowledge based system'          THEN 'expert systems'
        WHEN 'knowledge based systems'         THEN 'expert systems'

        WHEN 'pattern recognition'             THEN 'pattern recognition'
        WHEN 'pr'                              THEN 'pattern recognition'

        WHEN 'gan'                             THEN 'generative ai'
        WHEN 'gans'                            THEN 'generative ai'
        WHEN 'generative adversarial network'  THEN 'generative ai'
        WHEN 'generative adversarial networks' THEN 'generative ai'
        WHEN 'llm'                             THEN 'large language models'
        WHEN 'llms'                            THEN 'large language models'
        WHEN 'large language model'            THEN 'large language models'
        WHEN 'transformer'                     THEN 'transformer models'
        WHEN 'transformers'                    THEN 'transformer models'

        -- ============================================================
        -- DATA SCIENCE & ANALYTICS
        -- ============================================================
        WHEN 'datascience'                     THEN 'data science'
        WHEN 'data sciences'                   THEN 'data science'
        WHEN 'data analytics'                  THEN 'data science'
        WHEN 'analytics'                       THEN 'data science'

        WHEN 'datamining'                      THEN 'data mining'
        WHEN 'knowledge discovery'             THEN 'data mining'
        WHEN 'kdd'                             THEN 'data mining'

        WHEN 'bigdata'                         THEN 'big data'
        WHEN 'big data analytics'              THEN 'big data'
        WHEN 'big data analysis'               THEN 'big data'
        WHEN 'bigdata in cloud computing'      THEN 'big data'

        WHEN 'data visualization'              THEN 'data visualisation'
        WHEN 'visualization'                   THEN 'data visualisation'

        WHEN 'predictive analytics'            THEN 'predictive modeling'
        WHEN 'predictive modelling'            THEN 'predictive modeling'
        WHEN 'forecasting'                     THEN 'predictive modeling'

        WHEN 'data modelling'                  THEN 'data modeling'

        WHEN 'cluster analysis'                THEN 'clustering'
        WHEN 'clustering algorithms'           THEN 'clustering'
        WHEN 'classification'                  THEN 'classification'

        -- ============================================================
        -- DATABASES & INFORMATION RETRIEVAL
        -- ============================================================
        WHEN 'db'                              THEN 'databases'
        WHEN 'dbms'                            THEN 'databases'
        WHEN 'database'                        THEN 'databases'
        WHEN 'database management system'      THEN 'databases'
        WHEN 'database management systems'     THEN 'databases'
        WHEN 'relational database'             THEN 'databases'
        WHEN 'relational databases'            THEN 'databases'

        WHEN 'nosql databases'                 THEN 'nosql'
        WHEN 'no sql'                          THEN 'nosql'

        WHEN 'datawarehouse'                   THEN 'data warehouse'
        WHEN 'data warehousing'                THEN 'data warehouse'

        WHEN 'ir'                              THEN 'information retrieval'
        WHEN 'i r'                             THEN 'information retrieval'
        WHEN 'search engine'                   THEN 'information retrieval'
        WHEN 'search engines'                  THEN 'information retrieval'

        WHEN 'recommender system'              THEN 'recommender systems'
        WHEN 'recommendation system'           THEN 'recommender systems'
        WHEN 'recommendation systems'          THEN 'recommender systems'

        WHEN 'ontologies'                      THEN 'ontology'
        WHEN 'semantic web'                    THEN 'ontology'
        WHEN 'semantic technology'             THEN 'ontology'
        WHEN 'semantic technologies'           THEN 'ontology'
        WHEN 'knowledge graph'                 THEN 'ontology'
        WHEN 'knowledge graphs'                THEN 'ontology'

        -- ============================================================
        -- CYBER SECURITY
        -- ============================================================
        WHEN 'cybersecurity'                   THEN 'cyber security'
        WHEN 'cybsecurity'                     THEN 'cyber security'
        WHEN 'cyber sec'                       THEN 'cyber security'
        WHEN 'cybersec'                        THEN 'cyber security'
        WHEN 'computer security'               THEN 'cyber security'
        WHEN 'it security'                     THEN 'cyber security'
        WHEN 'information security'            THEN 'cyber security'
        WHEN 'infosec'                         THEN 'cyber security'
        WHEN 'network security'                THEN 'cyber security'
        WHEN 'cybercrime'                      THEN 'cyber security'
        WHEN 'cyber crime'                     THEN 'cyber security'

        WHEN 'cryptography'                    THEN 'cryptography'
        WHEN 'crypto'                          THEN 'cryptography'
        WHEN 'encryption'                      THEN 'cryptography'

        WHEN 'ids'                             THEN 'intrusion detection'
        WHEN 'i d s'                           THEN 'intrusion detection'
        WHEN 'intrusion detection system'      THEN 'intrusion detection'
        WHEN 'intrusion detection systems'     THEN 'intrusion detection'
        WHEN 'anomaly detection'               THEN 'intrusion detection'

        WHEN 'digital forensics'               THEN 'digital forensics'
        WHEN 'computer forensics'              THEN 'digital forensics'
        WHEN 'forensics'                       THEN 'digital forensics'

        WHEN 'penetration testing'             THEN 'penetration testing'
        WHEN 'pentest'                         THEN 'penetration testing'
        WHEN 'pen test'                        THEN 'penetration testing'
        WHEN 'ethical hacking'                 THEN 'penetration testing'

        WHEN 'malware'                         THEN 'malware analysis'
        WHEN 'malware detection'               THEN 'malware analysis'

        WHEN 'privacy'                         THEN 'data privacy'
        WHEN 'data protection'                 THEN 'data privacy'

        -- ============================================================
        -- COMPUTER NETWORKS
        -- ============================================================
        WHEN 'network'                         THEN 'computer networks'
        WHEN 'networks'                        THEN 'computer networks'
        WHEN 'computer network'                THEN 'computer networks'
        WHEN 'networking'                      THEN 'computer networks'
        WHEN 'network programming'             THEN 'computer networks'

        WHEN 'wireless'                        THEN 'wireless networks'
        WHEN 'wireless network'                THEN 'wireless networks'
        WHEN 'wireless communication'          THEN 'wireless networks'
        WHEN 'wireless communications'         THEN 'wireless networks'
        WHEN 'wsn'                             THEN 'wireless sensor networks'
        WHEN 'wireless sensor network'         THEN 'wireless sensor networks'

        WHEN 'mobile networks'                 THEN 'mobile networks'
        WHEN '5g'                              THEN 'mobile networks'
        WHEN '4g'                              THEN 'mobile networks'
        WHEN '6g'                              THEN 'mobile networks'

        WHEN 'sdn'                             THEN 'software defined networking'
        WHEN 'software defined network'        THEN 'software defined networking'
        WHEN 'software defined networks'       THEN 'software defined networking'

        WHEN 'noc'                             THEN 'network on chip'
        WHEN 'network on chip'                 THEN 'network on chip'

        -- ============================================================
        -- INTERNET OF THINGS / SMART SYSTEMS
        -- ============================================================
        WHEN 'iot'                             THEN 'internet of things'
        WHEN 'i o t'                           THEN 'internet of things'
        WHEN 'iot s'                           THEN 'internet of things'
        WHEN 'internet of things iot'          THEN 'internet of things'

        WHEN 'iiot'                            THEN 'industrial iot'
        WHEN 'industrial internet of things'   THEN 'industrial iot'

        WHEN 'smart city'                      THEN 'smart cities'
        WHEN 'smart home'                      THEN 'smart homes'

        -- ============================================================
        -- CLOUD / EDGE / FOG COMPUTING
        -- ============================================================
        WHEN 'cloud'                           THEN 'cloud computing'
        WHEN 'cloudcomputing'                  THEN 'cloud computing'
        WHEN 'cloud comp'                      THEN 'cloud computing'
        WHEN 'cloud services'                  THEN 'cloud computing'

        WHEN 'edge'                            THEN 'edge computing'
        WHEN 'edgecomputing'                   THEN 'edge computing'
        WHEN 'mobile edge cloud computing'     THEN 'edge computing'
        WHEN 'mobile edge computing'           THEN 'edge computing'
        WHEN 'mec'                             THEN 'edge computing'

        WHEN 'fog'                             THEN 'fog computing'
        WHEN 'fogcomputing'                    THEN 'fog computing'

        WHEN 'virtualization'                  THEN 'virtualisation'

        WHEN 'distributed computing'           THEN 'distributed systems'
        WHEN 'distributed system'              THEN 'distributed systems'

        WHEN 'parallel computing'              THEN 'parallel computing'
        WHEN 'hpc'                             THEN 'high performance computing'
        WHEN 'high performance computing'      THEN 'high performance computing'

        -- ============================================================
        -- SOFTWARE ENGINEERING
        -- ============================================================
        WHEN 'se'                              THEN 'software engineering'
        WHEN 's e'                             THEN 'software engineering'
        WHEN 'software engineer'               THEN 'software engineering'
        WHEN 'softwareengineering'             THEN 'software engineering'

        WHEN 'requirements engineering'        THEN 'requirements engineering'
        WHEN 're'                              THEN 'requirements engineering'
        WHEN 'software requirements'           THEN 'requirements engineering'

        WHEN 'software testing'                THEN 'software testing'
        WHEN 'testing'                         THEN 'software testing'
        WHEN 'qa'                              THEN 'software testing'
        WHEN 'quality assurance'               THEN 'software testing'

        WHEN 'agile'                           THEN 'agile methodologies'
        WHEN 'scrum'                           THEN 'agile methodologies'
        WHEN 'agile development'               THEN 'agile methodologies'

        WHEN 'devops'                          THEN 'devops'
        WHEN 'continuous integration'          THEN 'devops'
        WHEN 'ci cd'                           THEN 'devops'

        WHEN 'software architecture'           THEN 'software architecture'
        WHEN 'enterprise architecture'         THEN 'enterprise architecture'
        WHEN 'togaf'                           THEN 'enterprise architecture'

        WHEN 'design pattern'                  THEN 'design patterns'

        WHEN 'formal methods'                  THEN 'formal methods'
        WHEN 'model checking'                  THEN 'formal methods'

        WHEN 'programming languages'           THEN 'programming languages'
        WHEN 'pl'                              THEN 'programming languages'

        -- ============================================================
        -- INFORMATION SYSTEMS / IT GOVERNANCE
        -- ============================================================
        WHEN 'is'                              THEN 'information systems'
        WHEN 'i s'                             THEN 'information systems'
        WHEN 'information system'              THEN 'information systems'
        WHEN 'mis'                             THEN 'information systems'
        WHEN 'management information systems'  THEN 'information systems'
        WHEN 'computer information systems'    THEN 'information systems'

        WHEN 'it governance'                   THEN 'it governance'
        WHEN 'cobit'                           THEN 'it governance'
        WHEN 'itil'                            THEN 'it governance'

        WHEN 'erp'                             THEN 'enterprise systems'
        WHEN 'enterprise resource planning'    THEN 'enterprise systems'

        WHEN 'technology acceptance'           THEN 'technology adoption'
        WHEN 'technology adoption'             THEN 'technology adoption'
        WHEN 'tam'                             THEN 'technology adoption'

        -- ============================================================
        -- HUMAN COMPUTER INTERACTION / UX
        -- ============================================================
        WHEN 'hci'                             THEN 'human computer interaction'
        WHEN 'h c i'                           THEN 'human computer interaction'
        WHEN 'human computer interaction'      THEN 'human computer interaction'

        WHEN 'ux'                              THEN 'user experience'
        WHEN 'u x'                             THEN 'user experience'
        WHEN 'user experience design'          THEN 'user experience'
        WHEN 'usability'                       THEN 'user experience'
        WHEN 'ui ux'                           THEN 'user experience'
        WHEN 'ux ui'                           THEN 'user experience'

        WHEN 'ui'                              THEN 'user interface'
        WHEN 'u i'                             THEN 'user interface'
        WHEN 'user interface design'           THEN 'user interface'

        -- ============================================================
        -- WEB / MOBILE / GAMING
        -- ============================================================
        WHEN 'web development'                 THEN 'web development'
        WHEN 'web dev'                         THEN 'web development'
        WHEN 'web programming'                 THEN 'web development'
        WHEN 'web application'                 THEN 'web development'
        WHEN 'web applications'                THEN 'web development'
        WHEN 'web technologies'                THEN 'web development'

        WHEN 'mobile development'              THEN 'mobile development'
        WHEN 'mobile dev'                      THEN 'mobile development'
        WHEN 'android'                         THEN 'mobile development'
        WHEN 'ios development'                 THEN 'mobile development'
        WHEN 'mobile application'              THEN 'mobile development'
        WHEN 'mobile applications'             THEN 'mobile development'
        WHEN 'mobile app development'          THEN 'mobile development'

        WHEN 'game development'                THEN 'game development'
        WHEN 'game dev'                        THEN 'game development'
        WHEN 'video games'                     THEN 'game development'
        WHEN 'game design'                     THEN 'game development'
        WHEN 'game theory'                     THEN 'game theory'

        -- ============================================================
        -- IMAGE / SIGNAL / MULTIMEDIA
        -- ============================================================
        WHEN 'image proc'                      THEN 'image processing'
        WHEN 'imageprocessing'                 THEN 'image processing'
        WHEN 'digital image processing'        THEN 'image processing'

        WHEN 'signal processing'               THEN 'signal processing'
        WHEN 'dsp'                             THEN 'signal processing'
        WHEN 'digital signal processing'       THEN 'signal processing'

        WHEN 'multimedia'                      THEN 'multimedia systems'
        WHEN 'multimedia systems'              THEN 'multimedia systems'
        WHEN 'multimedia docuemnt indexation'  THEN 'multimedia systems'
        WHEN 'multimedia document indexation'  THEN 'multimedia systems'

        WHEN 'audio processing'                THEN 'audio processing'

        -- ============================================================
        -- ROBOTICS / AUTONOMOUS
        -- ============================================================
        WHEN 'robot'                           THEN 'robotics'
        WHEN 'robots'                          THEN 'robotics'
        WHEN 'robotic'                         THEN 'robotics'

        WHEN 'autonomous systems'              THEN 'autonomous systems'
        WHEN 'autonomous vehicles'             THEN 'autonomous systems'
        WHEN 'self driving'                    THEN 'autonomous systems'

        WHEN 'human robot interaction'         THEN 'human robot interaction'
        WHEN 'hri'                             THEN 'human robot interaction'

        -- ============================================================
        -- HEALTH / BIO INFORMATICS
        -- ============================================================
        WHEN 'bioinformatics'                  THEN 'bioinformatics'
        WHEN 'computational biology'           THEN 'bioinformatics'
        WHEN 'biocomputing'                    THEN 'bioinformatics'

        WHEN 'health informatics'              THEN 'health informatics'
        WHEN 'medical informatics'             THEN 'health informatics'
        WHEN 'e health'                        THEN 'health informatics'
        WHEN 'ehealth'                         THEN 'health informatics'
        WHEN 'digital health'                  THEN 'health informatics'

        WHEN 'medical imaging'                 THEN 'medical imaging'

        -- ============================================================
        -- EDUCATION / E-LEARNING
        -- ============================================================
        WHEN 'e learning'                      THEN 'educational technology'
        WHEN 'elearning'                       THEN 'educational technology'
        WHEN 'edtech'                          THEN 'educational technology'
        WHEN 'educational technology'          THEN 'educational technology'
        WHEN 'computer science education'      THEN 'educational technology'
        WHEN 'cs education'                    THEN 'educational technology'
        WHEN 'computing education'             THEN 'educational technology'
        WHEN 'distance learning'               THEN 'educational technology'
        WHEN 'online learning'                 THEN 'educational technology'
        WHEN 'moocs'                           THEN 'educational technology'

        -- ============================================================
        -- BLOCKCHAIN / FINTECH
        -- ============================================================
        WHEN 'blockchain'                      THEN 'blockchain'
        WHEN 'block chain'                     THEN 'blockchain'
        WHEN 'distributed ledger'              THEN 'blockchain'
        WHEN 'cryptocurrency'                  THEN 'blockchain'
        WHEN 'smart contracts'                 THEN 'blockchain'
        WHEN 'smart contract'                  THEN 'blockchain'

        WHEN 'fintech'                         THEN 'fintech'
        WHEN 'financial technology'            THEN 'fintech'

        -- ============================================================
        -- OS / SYSTEMS / EMBEDDED
        -- ============================================================
        WHEN 'os'                              THEN 'operating systems'
        WHEN 'o s'                             THEN 'operating systems'
        WHEN 'operating system'                THEN 'operating systems'

        WHEN 'embedded'                        THEN 'embedded systems'
        WHEN 'embedded system'                 THEN 'embedded systems'

        WHEN 'real time systems'               THEN 'real time systems'
        WHEN 'realtime'                        THEN 'real time systems'

        -- ============================================================
        -- THEORY / MATH / OPTIMISATION
        -- ============================================================
        WHEN 'optimization'                    THEN 'optimisation'
        WHEN 'optimisation'                    THEN 'optimisation'
        WHEN 'mathematical optimization'       THEN 'optimisation'
        WHEN 'combinatorial optimization'      THEN 'optimisation'
        WHEN 'computational offloading decision' THEN 'optimisation'
        WHEN 'optimal stopping'                THEN 'optimisation'
        WHEN 'mcdm'                            THEN 'multi criteria decision making'
        WHEN 'multi criteria decision making'  THEN 'multi criteria decision making'

        WHEN 'algorithms'                      THEN 'algorithms'
        WHEN 'algorithm'                       THEN 'algorithms'
        WHEN 'algorithm design'                THEN 'algorithms'

        WHEN 'graph theory'                    THEN 'graph theory'
        WHEN 'graphs'                          THEN 'graph theory'

        WHEN 'statistics'                      THEN 'statistics'
        WHEN 'statistical analysis'            THEN 'statistics'
        WHEN 'applied statistics'              THEN 'statistics'

        WHEN 'spatiotemporal traffic data'     THEN 'spatiotemporal analytics'
        WHEN 'spatio temporal'                 THEN 'spatiotemporal analytics'

        -- ============================================================
        -- BUSINESS / IS-RELATED
        -- ============================================================
        WHEN 'crm'                             THEN 'customer relationship management'
        WHEN 'customer relationship management' THEN 'customer relationship management'

        WHEN 'bpm'                             THEN 'business process management'
        WHEN 'business process management'     THEN 'business process management'

        WHEN 'project management'              THEN 'project management'
        WHEN 'pm'                              THEN 'project management'

        -- ============================================================
        -- DEFAULT — return the normalised lowercase form
        -- ============================================================
        ELSE norm
    END;
END;
$$;

COMMENT ON FUNCTION canonicalize_interest(text) IS
'Normalises research-interest labels — abbreviations, typos, synonyms, casing, punctuation — into a canonical form for set-overlap matching. Raw labels are preserved in Researcher.ResearchInterests for display.';
